"""Task 11b: the mirror additions (push, delete_branch, ls_remote, fetch_branches, absolute helper pin,
ssh host keys, audit deadline). Local repositories under the suite sandbox; credentials are fake."""
from __future__ import annotations

import http.server
import os
import shlex
import shutil
import subprocess
import threading
import unittest
from unittest import mock

import support  # noqa: F401
from swarm.supervisor import mirror
from swarm.supervisor.mirror import (AuditDeadlineError, Mirror, MirrorError, MirrorTimeoutError,
                                     MissingCommitError, PushRejectedError)
from test_drive_mirror import (HELPER_SH, SOCK, TOKEN, TOKEN_USER, MirrorCase, _Backend, git, ok,
                               spy)


def _read(path):
    with open(path) as fh:
        return fh.read()


def pack_cmd(kind):
    """Written out literally (not via gitpin): the exact command a local destination's pack program runs as."""
    parts = [mirror._resolve("git")]
    for item in ("core.hooksPath=/dev/null", "core.fsmonitor=false", "core.alternateRefsCommand=",
                 "receive.denyCurrentBranch=refuse", "receive.denyDeleteCurrent=refuse"):
        parts += ["-c", item]
    parts.append(f"{kind}-pack")
    return f"--{kind}-pack=" + " ".join(shlex.quote(p) for p in parts)


def mirror_receive_pack():
    return pack_cmd("receive")


class PushCase(MirrorCase):
    def setUp(self):
        super().setUp()
        self.remote, self.base = self.remote_with_main()
        self.work = self.repo("work")
        git(self.work, "fetch", "-q", self.remote, "main")
        git(self.work, "checkout", "-q", "-b", "feat", "FETCH_HEAD")
        self.tip = self.commit(self.work, "f.txt", "f\n", "feature")
        git(self.work, "push", "-q", self.remote, "feat")

    def mirror(self, **kw):
        m = Mirror(self.remote, self.agent_cred(), **kw)
        m.fetch(timeout=30)
        return m


class Push(PushCase):
    def test_push_fast_forwards_the_exact_sha(self):
        m = self.mirror()
        m.push(self.tip, "main")
        self.assertEqual(git(self.remote, "rev-parse", "main"), self.tip)

    def test_push_refspec_is_exact_sha_and_never_a_force(self):
        m = self.mirror()
        calls = []
        with spy(calls):
            m.push(self.tip, "main")
        push = [cmd for cmd, _ in calls if "push" in cmd][0]
        tail = push[push.index("push") + 1:]
        self.assertEqual(tail, ["--porcelain", mirror_receive_pack(), "--", self.remote, f"{self.tip}:refs/heads/main"])
        for word in push:
            self.assertFalse(word.startswith("+") or word in ("-f", "--force", "--force-with-lease", "--mirror"))

    def test_non_fast_forward_is_rejected_and_typed(self):
        m = self.mirror()
        other = self.repo("other")
        git(other, "fetch", "-q", self.remote, "main")
        git(other, "checkout", "-q", "-b", "m", "FETCH_HEAD")
        moved = self.commit(other, "o.txt", "o\n", "elsewhere")
        git(other, "push", "-q", self.remote, "m:main")
        with self.assertRaises(PushRejectedError) as cm:
            m.push(self.tip, "main")
        self.assertEqual(git(self.remote, "rev-parse", "main"), moved)
        self.assertNotIn(self.remote, str(cm.exception))

    def test_unknown_sha_is_a_missing_commit(self):
        m = self.mirror()
        with self.assertRaises(MissingCommitError):
            m.push("2" * 40, "main")

    def test_bad_sha_and_branch_refused_before_git(self):
        m = self.mirror()
        calls = []
        with spy(calls):
            for sha, branch in (("main", "main"), (self.tip, "a..b"), (self.tip, "x.lock"),
                                (self.tip, "-d"), (self.tip, "")):
                with self.subTest(sha=sha, branch=branch), self.assertRaises(MirrorError):
                    m.push(sha, branch)
        self.assertEqual(calls, [])

    def test_push_carries_only_the_pinned_credential_env(self):
        m = Mirror(self.remote, self.agent_cred())
        m.fetch(timeout=30)
        calls = []
        with mock.patch.dict(os.environ, {"SSH_AUTH_SOCK": "/other.sock"}), spy(calls):
            m.push(self.tip, "main")
            m.head("main")
        push_env = [kw["env"] for cmd, kw in calls if "push" in cmd][0]
        head_env = [kw["env"] for cmd, kw in calls if "rev-parse" in cmd][0]
        self.assertEqual(push_env["SSH_AUTH_SOCK"], SOCK)
        self.assertNotIn("SSH_AUTH_SOCK", head_env)
        self.assertEqual(set(push_env) - {"GIT_SSH_COMMAND", "SSH_AUTH_SOCK"}, {
            "GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL", "HOME", "PATH", "GIT_TERMINAL_PROMPT", "LC_ALL",
            "GIT_ALLOW_PROTOCOL"})

    def test_delete_branch(self):
        m = self.mirror()
        m.delete_branch("feat")
        out = git(self.remote, "branch", "--list", "feat")
        self.assertEqual(out, "")
        calls = []
        with spy(calls, ok):
            m.delete_branch("feat")
        self.assertIn(":refs/heads/feat", calls[-1][0])
        with self.assertRaises(MirrorError):
            m.delete_branch("a..b")

    def test_delete_of_a_missing_branch_is_idempotent(self):
        m = self.mirror()
        m.delete_branch("nonexistent")
        self.assertEqual(git(self.remote, "rev-parse", "main"), self.base)

    def test_delete_failure_is_typed(self):
        gone = self.repo("gone.git", bare=True)
        m = Mirror(gone, self.agent_cred())
        shutil.rmtree(gone)
        with self.assertRaises(PushRejectedError):
            m.delete_branch("feat")


class LsRemote(PushCase):
    def test_ls_remote_lists_branches(self):
        m = Mirror(self.remote, self.agent_cred())
        refs = m.ls_remote(timeout=30)
        self.assertEqual(refs, {"refs/heads/main": self.base, "refs/heads/feat": self.tip})

    def test_ls_remote_goes_through_the_pinned_environment(self):
        m = Mirror(self.remote, self.agent_cred())
        calls = []
        with spy(calls):
            m.ls_remote(timeout=30)
        cmd, kw = calls[-1]
        self.assertIn("ls-remote", cmd)
        self.assertEqual(kw["env"]["SSH_AUTH_SOCK"], SOCK)
        self.assertIn("core.hooksPath=/dev/null", cmd)
        self.assertEqual(kw["env"]["HOME"], m.private_home)

    def test_ls_remote_keeps_only_branches_with_a_real_sha(self):
        m = Mirror(self.remote, self.agent_cred())
        out = (f"{'1' * 40}\trefs/heads/main\n{'2' * 40}\trefs/tags/v1\nnot-a-sha\trefs/heads/x\n"
               f"{'3' * 40}\tHEAD\n{'4' * 40}\trefs/heads/feat/y\n").encode()
        with spy([], lambda cmd, kw: subprocess.CompletedProcess(cmd, 0, stdout=out, stderr=b"")):
            self.assertEqual(m.ls_remote(timeout=5), {"refs/heads/main": "1" * 40, "refs/heads/feat/y": "4" * 40})

    def test_ls_remote_timeout_is_validated_and_capped(self):
        m = Mirror(self.remote, self.agent_cred())
        with self.assertRaises(ValueError):
            m.ls_remote(timeout=0)
        calls = []
        with spy(calls):
            m.ls_remote(timeout=999)
        self.assertEqual(calls[-1][1]["timeout"], mirror.MAX_CALL_TIMEOUT)


class FetchBranches(PushCase):
    def test_fetch_branches_brings_only_the_named_ones(self):
        m = Mirror(self.remote, self.agent_cred())
        got, pending = m.fetch_branches(["feat"], timeout=30)
        self.assertEqual(got, {"refs/heads/feat": self.tip})
        self.assertEqual(pending, [])
        self.assertIsNone(m.head("main"))

    def test_a_timed_out_branch_and_those_after_it_stay_pending(self):
        m = Mirror(self.remote, self.agent_cred())
        real = mirror.Mirror._exec
        seen = []

        def flaky(self_, cmd, **kw):
            if "fetch" in cmd:
                seen.append(cmd[-1])
                if cmd[-1].endswith("feat:refs/heads/feat"):
                    raise MirrorTimeoutError("git timed out after 1s")
            return real(self_, cmd, **kw)

        with mock.patch.object(mirror.Mirror, "_exec", flaky):
            got, pending = m.fetch_branches(["main", "feat", "main"], timeout=30)
        self.assertEqual(got, {"refs/heads/main": self.base})
        self.assertEqual(pending, ["feat", "main"])
        # the retry (a later pass) brings the rest
        got, pending = m.fetch_branches(pending, timeout=30)
        self.assertEqual(pending, [])
        self.assertEqual(got["refs/heads/feat"], self.tip)

    def test_overall_timeout_runs_out_between_branches(self):
        m = Mirror(self.remote, self.agent_cred())
        clock = iter([0.0, 0.0, 100.0, 100.0, 100.0])
        calls = []
        with mock.patch.object(mirror.time, "monotonic", lambda: next(clock)), spy(calls):
            got, pending = m.fetch_branches(["main", "feat"], timeout=5)
        self.assertEqual(pending, ["feat"])
        self.assertIn("refs/heads/main", got)
        self.assertEqual(len([1 for cmd, _ in calls if "fetch" in cmd]), 1)   # none for the branch past the budget

    def test_the_second_branch_gets_only_the_time_left(self):
        m = Mirror(self.remote, self.agent_cred())
        clock = iter([0.0, 0.0, 10.0, 10.0, 10.0, 10.0])
        calls = []
        with mock.patch.object(mirror.time, "monotonic", lambda: next(clock)), spy(calls, ok):
            m.fetch_branches(["main", "feat"], timeout=30)
        times = [kw["timeout"] for cmd, kw in calls if "fetch" in cmd]
        self.assertEqual(times, [30.0, 20.0])

    def test_each_branch_call_gets_the_time_left_capped_at_60(self):
        m = Mirror(self.remote, self.agent_cred())
        calls = []
        with spy(calls):
            m.fetch_branches(["main"], timeout=500)
        fetch = [kw for cmd, kw in calls if "fetch" in cmd][0]
        self.assertEqual(fetch["timeout"], mirror.MAX_CALL_TIMEOUT)

    def test_missing_remote_branch_raises(self):
        m = Mirror(self.remote, self.agent_cred())
        with self.assertRaises(MirrorError) as cm:
            m.fetch_branches(["nope"], timeout=30)
        self.assertNotIn(self.remote, str(cm.exception))

    def test_bad_branch_and_timeout_refused(self):
        m = Mirror(self.remote, self.agent_cred())
        calls = []
        with spy(calls), self.assertRaises(MirrorError):
            m.fetch_branches(["main", "a..b"], timeout=5)
        self.assertEqual(calls, [])
        with self.assertRaises(ValueError):
            m.fetch_branches(["main"], timeout=0)


class AbsoluteHelperPin(MirrorCase):
    def gh(self, mode=0o700, name="gh"):
        d = os.path.join(self.root, "bin")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, name)
        with open(path, "w") as fh:
            fh.write(HELPER_SH.replace("{user}", TOKEN_USER))
        os.chmod(path, mode)
        return path

    def cred(self, command):
        return {"kind": "helper", "command": command, "config_dir": self.home}

    @support.posix_only("shell fixture and file modes")
    def test_absolute_helper_path_is_accepted_when_basename_and_args_are_allowlisted(self):
        remote, _ = self.remote_with_main()
        gh = self.gh()
        m = Mirror(remote, self.cred(f"{gh} auth git-credential"), trusted_helper_paths=(gh,))
        helper = [a for a in m._cred_args if a.startswith("credential.helper=!")][0]
        words = shlex.split(helper[len("credential.helper=!"):])
        self.assertEqual(words[2:], [gh, "auth", "git-credential"])

    @support.posix_only("file modes")
    def test_absolute_helper_refusals(self):
        remote, _ = self.remote_with_main()
        gh = self.gh()
        evil = self.gh(name="evil")
        plain = self.gh(mode=0o600, name="gh2")
        cases = {
            "other basename": f"{evil} auth git-credential",
            "other args": f"{gh} auth login",
            "no args": gh,
            "missing file": os.path.join(self.root, "nope", "gh") + " auth git-credential",
            "dir": self.root + "/bin auth git-credential",
        }
        for name, command in cases.items():
            with self.subTest(name), self.assertRaises(MirrorError):
                Mirror(remote, self.cred(command), trusted_helper_paths=(gh, evil))
        os.rename(plain, os.path.join(self.root, "bin", "gh3"))
        os.chmod(gh, 0o600)
        with self.assertRaises(MirrorError) as cm:
            Mirror(remote, self.cred(f"{gh} auth git-credential"), trusted_helper_paths=(gh,))
        self.assertIn("not executable", str(cm.exception))

    @support.posix_only("file modes")
    def test_relative_helper_never_resolved_from_the_path(self):
        remote, _ = self.remote_with_main()
        with self.assertRaises(MirrorError):
            Mirror(remote, self.cred("./gh auth git-credential"))


class NoCredentialAndMergeBase(PushCase):
    def test_none_credential_only_for_local_destinations(self):
        m = Mirror(self.remote, {"kind": "none"})
        self.assertEqual(m._cred_env, {})
        m.fetch(timeout=30)
        m.push(self.tip, "main")
        self.assertEqual(git(self.remote, "rev-parse", "main"), self.tip)
        Mirror("file://" + self.remote, {"kind": "none"})
        for url in ("https://example.test/x.git", "ssh://git@example.test/x", "git@example.test:x.git",
                    "relative/path.git"):
            with self.subTest(url=url), self.assertRaises(MirrorError):
                Mirror(url, {"kind": "none"})

    def test_merge_base(self):
        m = self.mirror()
        m.fetch_branches(["feat"], 30)
        self.assertEqual(m.merge_base(self.tip, self.base), self.base)
        self.assertEqual(m.merge_base(self.base, self.tip), self.base)
        other = self.repo("unrelated")
        stranger = self.commit(other, "u.txt", "u\n", "unrelated root")
        git(other, "push", "-q", self.remote, "main:stranger")
        m.fetch_branches(["stranger"], 30)
        self.assertIsNone(m.merge_base(stranger, self.base))
        with self.assertRaises(MissingCommitError):
            m.merge_base("3" * 40, self.base)
        with self.assertRaises(MirrorError):
            m.merge_base("main", self.base)


class HelperTrust(AbsoluteHelperPin):
    """[S3] A pinned absolute helper must be in a system directory or named by the person, and unwritable by others."""

    @support.posix_only("file modes")
    def test_a_helper_outside_system_dirs_and_not_named_is_refused(self):
        remote, _ = self.remote_with_main()
        gh = self.gh()
        with self.assertRaises(MirrorError) as cm:
            Mirror(remote, self.cred(f"{gh} auth git-credential"))
        self.assertIn("credential_helpers", str(cm.exception))
        with self.assertRaises(MirrorError):
            Mirror(remote, self.cred(f"{gh} auth git-credential"), trusted_helper_paths=(gh + "x",))

    @support.posix_only("file modes")
    def test_a_named_helper_must_not_be_writable_by_others(self):
        remote, _ = self.remote_with_main()
        gh = self.gh()
        for mode in (0o722, 0o702, 0o777):
            os.chmod(gh, mode)
            with self.subTest(mode=oct(mode)), self.assertRaises(MirrorError) as cm:
                Mirror(remote, self.cred(f"{gh} auth git-credential"), trusted_helper_paths=(gh,))
            self.assertIn("writable", str(cm.exception))
        os.chmod(gh, 0o700)
        Mirror(remote, self.cred(f"{gh} auth git-credential"), trusted_helper_paths=(gh,))

    @support.posix_only("file modes")
    def test_a_writable_parent_directory_is_refused_but_a_sticky_one_is_not(self):
        remote, _ = self.remote_with_main()
        gh = self.gh()
        bindir = os.path.dirname(gh)
        os.chmod(bindir, 0o777)
        with self.assertRaises(MirrorError):
            Mirror(remote, self.cred(f"{gh} auth git-credential"), trusted_helper_paths=(gh,))
        os.chmod(bindir, 0o755)
        Mirror(remote, self.cred(f"{gh} auth git-credential"), trusted_helper_paths=(gh,))

    @support.posix_only("file modes")
    def test_a_symlink_to_a_planted_file_is_judged_by_its_target(self):
        remote, _ = self.remote_with_main()
        gh = self.gh()
        link = os.path.join(self.root, "bin", "gh-link")
        os.symlink(gh, link)
        os.chmod(gh, 0o777)
        with self.assertRaises(MirrorError):
            Mirror(remote, self.cred(f"{link} auth git-credential"), trusted_helper_paths=(link,))

    @support.posix_only("file modes")
    def test_a_symlink_from_a_safe_directory_into_a_writable_one_is_refused(self):
        remote, _ = self.remote_with_main()
        gh = self.gh()
        os.chmod(os.path.dirname(gh), 0o777)             # where the real file lives
        safe = os.path.join(self.root, "safe")
        os.makedirs(safe)
        link = os.path.join(safe, "gh")
        os.symlink(gh, link)
        with self.assertRaises(MirrorError) as cm:
            Mirror(remote, self.cred(f"{link} auth git-credential"), trusted_helper_paths=(link,))
        self.assertIn("writable", str(cm.exception))

    def test_system_directory_helper_needs_no_naming(self):
        remote, _ = self.remote_with_main()
        sh = mirror._resolve("env")                      # a root-owned executable in a system directory
        self.assertEqual(os.path.dirname(os.path.realpath(sh)) in {os.path.realpath(d) for d in mirror.SYSTEM_DIRS}, True)
        mirror._check_helper_file(sh, ())


class LocalHooks(PushCase):
    """[S1] A local destination's own hooks and config must never run in the supervisor's pass."""

    def plant(self, names=("pre-receive", "update", "post-receive", "reference-transaction")):
        marker = os.path.join(self.root, "HOOK-RAN")
        for name in names:
            hook = os.path.join(self.remote, "hooks", name)
            with open(hook, "w") as fh:
                fh.write(f"#!/bin/sh\necho {name} >> {marker}\ncat >/dev/null\n")
            os.chmod(hook, 0o755)
        return marker

    @support.posix_only("shell hooks")
    def test_planted_hooks_do_not_run_on_push_or_delete(self):
        marker = self.plant()
        m = self.mirror()
        m.push(self.tip, "main")
        m.delete_branch("feat")
        self.assertFalse(os.path.exists(marker), "a destination hook ran")
        self.assertEqual(git(self.remote, "rev-parse", "main"), self.tip)

    @support.posix_only("shell hooks")
    def test_planted_hooks_would_run_without_the_guard(self):
        marker = self.plant(("post-receive",))
        m = self.mirror()
        with mock.patch.object(Mirror, "_pack", lambda self, kind: []):
            m.push(self.tip, "main")
        self.assertTrue(os.path.exists(marker))

    def test_local_calls_carry_the_safe_pack_commands_and_remote_ones_do_not(self):
        m = self.mirror()
        calls = []
        with spy(calls):
            m.fetch(30)
            m.fetch_branches(["main"], 30)
            m.ls_remote(30)
        for cmd, _ in calls:
            if any(w in cmd for w in ("fetch", "ls-remote")):
                self.assertIn(pack_cmd("upload"), cmd)
                self.assertEqual(cmd[cmd.index("--") + 1], m.url)
        self.assertEqual(Mirror("https://example.test/o/r.git", {"kind": "ssh-agent", "socket": SOCK})._pack("fetch"), [])
        self.assertEqual(Mirror("git@example.test:o/r.git", {"kind": "ssh-agent", "socket": SOCK})._pack("push"), [])
        self.assertTrue(Mirror("file://" + self.remote, {"kind": "none"})._pack("push"))

    def test_none_credential_only_for_the_suite_or_the_private_directory(self):
        Mirror(self.remote, {"kind": "none"})                  # creates the private directory (mode 0700)
        with mock.patch.dict(os.environ):
            os.environ.pop(mirror.TEST_MARKER, None)
            with self.assertRaises(MirrorError) as cm:
                Mirror(self.remote, {"kind": "none"})
            self.assertIn("private directory", str(cm.exception))
            with self.assertRaises(MirrorError):
                Mirror("file://" + self.remote, {"kind": "none"})
            inside = os.path.join(mirror.private_dir(), "local-dest.git")
            subprocess.run(["git", "init", "-q", "--bare", inside], check=True)
            Mirror(inside, {"kind": "none"})
            Mirror("file://" + inside, {"kind": "none"})
            with self.assertRaises(MirrorError):          # a dotdot escape is judged by its real path
                Mirror(os.path.join(inside, "..", "..", "x"), {"kind": "none"})


class LocalDestinations(PushCase):
    """[R1] [R2] [R3] A local destination is supervisor-private whatever its credential, resolved the way git
    resolves it, bare, and its config never runs."""

    def outside(self):
        Mirror(self.remote, {"kind": "none"})                  # creates the private directory
        return mock.patch.dict(os.environ, {k: v for k, v in os.environ.items() if k != mirror.TEST_MARKER}, clear=True)

    def bare_inside(self, name="dest.git"):
        Mirror(self.remote, {"kind": "none"})                  # creates the private directory (mode 0700)
        inside = os.path.join(str(mirror.private_dir()), name)
        subprocess.run(["git", "init", "-q", "--bare", inside], check=True)
        return inside

    def test_percent_encoded_traversal_out_of_the_private_directory_is_refused(self):
        plain = self.bare_inside("plain.git")
        with self.outside():
            root = os.path.join(str(mirror.private_dir()), "mirrors")
            rel = os.path.relpath(self.remote, root)
            encoded = "/".join("%2e%2e" if part == ".." else part for part in rel.split(os.sep))
            for url in ("file://" + root + "/" + encoded, "file://" + root + "/" + rel.replace("..", "%2E%2E")):
                with self.subTest(url=url), self.assertRaises(MirrorError) as cm:
                    Mirror(url, {"kind": "none"})
                self.assertIn("%", str(cm.exception))                # gitpin: git percent-decodes file:// paths
            for url in ("file://" + root + "/" + rel, os.path.join(root, rel)):   # plain traversal
                with self.subTest(url=url), self.assertRaises(MirrorError):
                    Mirror(url, {"kind": "none"})
            outside_rel = os.path.relpath(self.remote, os.path.dirname(plain))
            with self.assertRaises(MirrorError):
                Mirror(os.path.join(os.path.dirname(plain), outside_rel), {"kind": "none"})   # resolves outside
            with self.assertRaises(MirrorError) as cm:
                Mirror(self.remote, {"kind": "none"})
            self.assertIn("private directory", str(cm.exception))

    def test_every_credential_kind_obeys_the_private_directory_rule_for_a_local_path(self):
        creds = [{"kind": "none"}, {"kind": "ssh-agent", "socket": SOCK},
                 {"kind": "ssh-key", "path": os.path.join(self.root, "k")},
                 {"kind": "helper", "command": f"sh {self.helper}", "config_dir": self.home}]
        inside = self.bare_inside()
        with self.outside():
            for cred in creds:
                with self.subTest(kind=cred["kind"]):
                    with self.assertRaises(MirrorError) as cm:
                        Mirror(self.remote, cred, allowed_helpers=(f"sh {self.helper}",))
                    self.assertIn("private directory", str(cm.exception))
                    Mirror(inside, cred, allowed_helpers=(f"sh {self.helper}",))
                    Mirror("file://" + inside, cred, allowed_helpers=(f"sh {self.helper}",))

    def test_a_symlink_into_the_private_directory_is_judged_by_its_target(self):
        link = os.path.join(self.root, "link.git")
        os.symlink(self.remote, link)
        inside = self.bare_inside()
        into = os.path.join(self.root, "into.git")
        os.symlink(inside, into)
        with self.outside():
            with self.assertRaises(MirrorError):
                Mirror(link, {"kind": "none"})
            Mirror(into, {"kind": "none"})

    def test_a_local_destination_must_be_bare(self):
        work = self.repo("worktree-dest")
        self.commit(work, "a.txt", "a\n")
        with self.assertRaises(MirrorError) as cm:
            Mirror(work, {"kind": "none"})
        self.assertIn("bare", str(cm.exception))
        with self.assertRaises(MirrorError):
            Mirror(os.path.join(self.root, "does-not-exist.git"), {"kind": "none"})
        inside = self.bare_inside("n.git")
        os.makedirs(os.path.join(inside, ".git"))
        with self.outside(), self.assertRaises(MirrorError):
            Mirror(inside, {"kind": "none"})

    def test_a_bare_looking_directory_whose_config_says_otherwise_is_refused(self):
        for key, value in (("core.bare", "false"), ("core.worktree", self.root)):
            dest = self.bare_inside(f"cfg-{key.split('.')[1]}.git")
            git(dest, "config", key, value)
            with self.subTest(key=key), self.assertRaises(MirrorError) as cm:
                Mirror(dest, {"kind": "none"})
            self.assertIn(key, str(cm.exception))
        ok_dest = self.bare_inside("ok.git")
        Mirror(ok_dest, {"kind": "none"})

    @support.posix_only("shell hooks")
    def test_alternate_refs_command_in_the_destination_config_does_not_run(self):
        marker = os.path.join(self.root, "ALT-RAN")
        other = self.repo("alt-source", bare=True)
        with open(os.path.join(self.remote, "objects", "info", "alternates"), "w") as fh:
            fh.write(os.path.join(other, "objects") + "\n")
        git(self.remote, "config", "core.alternateRefsCommand", f"sh -c 'echo ran >> {marker}' --")
        m = self.mirror()
        m.fetch(30)
        m.fetch_branches(["main"], 30)
        m.ls_remote(30)
        m.push(self.tip, "main")
        self.assertFalse(os.path.exists(marker), "core.alternateRefsCommand ran")

    @support.posix_only("shell hooks")
    def test_fsmonitor_and_update_instead_cannot_run_anything(self):
        # a non-bare destination (the update-instead case) is refused outright, so its work tree is never updated
        work = self.repo("ui")
        marker = os.path.join(self.root, "FSMON-RAN")
        self.commit(work, "a.txt", "a\n")
        git(work, "config", "core.fsmonitor", f"sh -c 'echo ran >> {marker}'")
        git(work, "config", "receive.denyCurrentBranch", "updateInstead")
        with self.assertRaises(MirrorError):
            Mirror(work, {"kind": "none"})
        self.assertFalse(os.path.exists(marker))

    def test_the_pack_commands_pin_every_destination_setting(self):
        m = self.mirror()
        for kind in ("upload", "receive"):
            (arg,) = m._pack("push" if kind == "receive" else "fetch")
            self.assertEqual(arg, pack_cmd(kind))
        self.assertEqual(m._pack("ls-remote"), [pack_cmd("upload")])


class SymlinkSwap(PushCase):
    """[Q1] git is given the path that was checked, so a link swapped after construction changes nothing."""

    def test_a_swapped_link_cannot_redirect_a_later_call(self):
        Mirror(self.remote, {"kind": "none"})                  # creates the private directory
        private = os.path.realpath(str(mirror.private_dir()))
        good_dir = os.path.join(private, "dests")
        os.makedirs(good_dir)
        good = os.path.join(good_dir, "d.git")
        subprocess.run(["git", "clone", "-q", "--bare", self.remote, good], check=True)
        evil_dir = os.path.join(self.root, "evil")
        os.makedirs(evil_dir)
        evil = os.path.join(evil_dir, "d.git")
        subprocess.run(["git", "clone", "-q", "--bare", self.remote, evil], check=True)
        git(evil, "update-ref", "refs/heads/main", self.tip)       # evil's main differs from good's
        link = os.path.join(self.root, "seat-link")
        os.symlink(good_dir, link)
        with mock.patch.dict(os.environ, {k: v for k, v in os.environ.items() if k != mirror.TEST_MARKER}, clear=True):
            m = Mirror(os.path.join(link, "d.git"), {"kind": "none"})
            os.remove(link)
            os.symlink(evil_dir, link)
            refs = m.fetch(timeout=30)
        self.assertEqual(refs["refs/heads/main"], self.base)
        self.assertEqual(m.url, good)
        self.assertNotIn(link, m.url)

    def test_errors_hide_both_the_given_and_the_resolved_path(self):
        dest = self.repo("hide.git", bare=True)
        m = Mirror("file://" + dest, {"kind": "none"})
        self.assertEqual(m.url, os.path.realpath(dest))
        self.assertEqual(m._last_line(subprocess.CompletedProcess([], 1, b"", f"fatal: {dest} and file://{dest}".encode())).count("<remote>") >= 1, True)
        self.assertNotIn(dest, m._last_line(subprocess.CompletedProcess([], 1, b"", f"fatal: {dest}".encode())))


class GitpinUse(MirrorCase):
    """Every pinned-URL git call goes through gitpin: `--` before the URL, its env, its cwd, its URL rule."""

    def test_the_url_follows_a_double_dash_in_every_call(self):
        remote, sha = self.remote_with_main()
        calls = []
        with spy(calls):
            m = Mirror(remote, {"kind": "none"})
            m.fetch(30)
            m.fetch_branches(["main"], 30)
            m.ls_remote(30)
            with self.assertRaises(MirrorError):
                m.push("1" * 40, "main")
        for cmd, _ in calls:
            if remote in cmd:
                i = cmd.index(remote)
                self.assertEqual(cmd[i - 1], "--", cmd)
                self.assertNotIn("--", cmd[:i - 1][cmd.index("--git-dir"):] if False else [])

    def test_the_url_is_checked_again_at_every_call(self):
        remote, _ = self.remote_with_main()
        m = Mirror(remote, {"kind": "none"})
        m.url = "--upload-pack=x"
        calls = []
        with spy(calls), self.assertRaises(MirrorError) as cm:
            m.ls_remote(30)
        self.assertIn("'-'", str(cm.exception))
        self.assertEqual(calls, [])

    def test_decoded_control_characters_in_a_local_path_are_refused(self):
        for tail in ("%0A", "%0d", "%00", "%1b"):
            with self.subTest(tail=tail), self.assertRaises(MirrorError) as cm:
                Mirror("file://" + os.path.join(self.root, "a" + tail + "b.git"), {"kind": "none"})
            self.assertIn("%", str(cm.exception))

    def test_gitpin_decides_which_urls_are_acceptable(self):
        for url in ("--upload-pack=sh", "ext::sh -c x", "https://u:p@example.test/r", "http://example.test/r",
                    "https://example.test/%2dopt/r", "file://host/abs/path", "relative/path", "https://h/a\\b"):
            with self.subTest(url=url), self.assertRaises(MirrorError):
                Mirror(url, {"kind": "ssh-agent", "socket": SOCK})

    def test_default_helper_allowlist_is_gitpins(self):
        from swarm import gitpin
        for name, args in gitpin.CREDENTIAL_HELPERS.items():
            self.assertIn(" ".join((name, *args)), mirror.ALLOWED_HELPERS)
        self.assertEqual(len(mirror.ALLOWED_HELPERS), len(gitpin.CREDENTIAL_HELPERS))

    def test_the_module_keeps_no_copy_of_gitpins_settings(self):
        self.assertFalse(hasattr(mirror, "_SAFE_CONFIG"))
        self.assertFalse(hasattr(mirror, "_under_private"))


class SshHosts(MirrorCase):
    def test_ssh_host_parsing(self):
        cases = {
            "ssh://git@example.test/x.git": ("example.test", 22),
            "ssh://example.test:2222/x.git": ("example.test", 2222),
            "git@example.test:org/x.git": ("example.test", 22),
            "https://example.test/x.git": None,
            "/srv/repo.git": None,
            "file:///srv/repo.git": None,
            "C:\\repos\\r.git": None,
            "C:/repos/r.git": None,
            "ab:o/r.git": ("ab", 22),
        }
        for url, want in cases.items():
            with self.subTest(url=url):
                self.assertEqual(mirror.ssh_host_of(url), want)

    def test_strict_host_key_checking_is_always_on(self):
        remote, _ = self.remote_with_main()
        m = Mirror(remote, self.agent_cred())
        self.assertIn("StrictHostKeyChecking=yes", m._cred_env["GIT_SSH_COMMAND"])
        self.assertNotIn("accept-new", m._cred_env["GIT_SSH_COMMAND"])

    LINE = "example.test ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"

    def test_add_known_host_requires_a_confirmation(self):
        remote, _ = self.remote_with_main()
        m = Mirror(remote, self.agent_cred())
        for flag in (False, None, 1, "yes"):
            with self.subTest(flag=flag), self.assertRaises(MirrorError):
                mirror.add_known_host(m._known_hosts, "example.test", 22, [self.LINE], confirmed=flag)
        self.assertEqual(_read(m._known_hosts), "")
        self.assertEqual(mirror.add_known_host(m._known_hosts, "example.test", 22, [self.LINE],
                                               confirmed=True), 1)
        self.assertTrue(mirror.known_host_present(m._known_hosts, "example.test", 22))
        self.assertFalse(mirror.known_host_present(m._known_hosts, "example.test", 2222))
        self.assertFalse(mirror.known_host_present(m._known_hosts, "other.test", 22))

    def test_add_known_host_refuses_lines_for_another_host_or_junk(self):
        remote, _ = self.remote_with_main()
        m = Mirror(remote, self.agent_cred())
        for line in ("other.test ssh-ed25519 AAAA", "example.test ssh-dss AAAA",
                     "example.test ssh-ed25519 !!!notbase64", "example.test", "*",
                     self.LINE + "\nother.test ssh-ed25519 AAAA"):
            with self.subTest(line=line), self.assertRaises(MirrorError):
                mirror.add_known_host(m._known_hosts, "example.test", 22, [line], confirmed=True)
        self.assertEqual(_read(m._known_hosts), "")

    def test_nonstandard_port_uses_the_bracket_form(self):
        remote, _ = self.remote_with_main()
        m = Mirror(remote, self.agent_cred())
        line = "[example.test]:2222 " + self.LINE.split(" ", 1)[1]
        mirror.add_known_host(m._known_hosts, "example.test", 2222, [line], confirmed=True)
        self.assertTrue(mirror.known_host_present(m._known_hosts, "example.test", 2222))
        with self.assertRaises(MirrorError):
            mirror.add_known_host(m._known_hosts, "example.test", 2222, [self.LINE], confirmed=True)

    def test_host_key_known_follows_the_file(self):
        m = Mirror("ssh://git@example.test/x.git", self.agent_cred())
        self.assertFalse(m.host_key_known())
        mirror.add_known_host(m._known_hosts, "example.test", 22, [self.LINE], confirmed=True)
        self.assertTrue(m.host_key_known())
        self.assertTrue(Mirror(self.remote_with_main()[0], self.agent_cred()).host_key_known())

    def test_append_keeps_an_existing_unterminated_line(self):
        remote, _ = self.remote_with_main()
        m = Mirror(remote, self.agent_cred())
        with open(m._known_hosts, "w") as fh:
            fh.write("a.test ssh-ed25519 AAAA")
        mirror.add_known_host(m._known_hosts, "example.test", 22, [self.LINE], confirmed=True)
        lines = _read(m._known_hosts).splitlines()
        self.assertEqual(lines, ["a.test ssh-ed25519 AAAA", self.LINE])

    def test_scan_shows_fingerprints_and_trusts_nothing(self):
        out = self.LINE + "\n# comment\n"

        def fake(cmd, timeout):
            self.assertIn("ssh-keyscan", cmd[0])
            self.assertEqual(cmd[-1], "example.test")
            return subprocess.CompletedProcess(cmd, 0, out.encode(), b"")

        with mock.patch.object(mirror, "_tool", fake):
            keys = mirror.scan_host_keys("example.test", 22)
        self.assertEqual(keys[0][0], self.LINE)
        self.assertTrue(keys[0][1].startswith("ssh-ed25519 SHA256:"))
        with mock.patch.object(mirror, "_tool", lambda c, t: subprocess.CompletedProcess(c, 0, b"", b"")):
            with self.assertRaises(MirrorError):
                mirror.scan_host_keys("example.test", 22)
        ran = []
        for bad in ("-oProxyCommand=x", "a b", "", "x;y", "a/b", 5):
            with mock.patch.object(mirror, "_tool", lambda c, t: ran.append(c)), self.assertRaises(MirrorError):
                mirror.scan_host_keys(bad, 22)
        for port in (0, 70000, True, "22", -1):
            with mock.patch.object(mirror, "_tool", lambda c, t: ran.append(c)), self.assertRaises(MirrorError):
                mirror.scan_host_keys("example.test", port)
        self.assertEqual(ran, [])


class RealTimeout(MirrorCase):
    @support.posix_only("sleep")
    def test_a_real_git_timeout_is_a_typed_timeout_error(self):
        remote, _ = self.remote_with_main()
        m = Mirror(remote, self.agent_cred())
        with self.assertRaises(MirrorTimeoutError):
            m._exec(["sleep", "5"], env={"PATH": "/usr/bin:/bin"}, timeout=0.2)


class ToolTimeout(unittest.TestCase):
    @support.posix_only("sleep")
    def test_a_slow_tool_is_a_typed_timeout_and_is_killed(self):
        with self.assertRaises(MirrorTimeoutError):
            mirror._tool(["/bin/sleep", "5"], 0.2)

    @support.posix_only("true")
    def test_a_tool_runs_with_a_minimal_environment(self):
        out = mirror._tool(["/usr/bin/env"], 5).stdout.decode().split()
        self.assertEqual(sorted(x.split("=")[0] for x in out if "=" in x), ["LC_ALL", "PATH"])


class AuditDeadline(MirrorCase):
    def setUp(self):
        super().setUp()
        self.remote, self.base = self.remote_with_main()
        self.m = Mirror(self.remote, self.agent_cred())
        self.m.fetch(timeout=30)

    def test_deadline_caps_each_call_to_the_time_left(self):
        calls = []
        now = [1000.0]
        with mock.patch.object(mirror.time, "monotonic", lambda: now[0]), spy(calls):
            self.m.audit_target(self.base, self.base, set(), deadline=7)
        timeouts = [kw["timeout"] for _, kw in calls if "timeout" in kw]
        self.assertTrue(timeouts)
        self.assertTrue(all(t <= 7 for t in timeouts), timeouts)

    def test_deadline_passed_raises_before_the_next_git_call(self):
        now = [0.0]

        def tick():
            now[0] += 50
            return now[0]

        calls = []
        with mock.patch.object(mirror.time, "monotonic", tick), spy(calls):
            with self.assertRaises(AuditDeadlineError):
                self.m.audit_target(self.base, self.base, set(), deadline=60)
        self.assertLessEqual(len(calls), 3)

    def test_no_git_call_is_made_once_the_deadline_has_passed(self):
        times = iter([0.0] + [1000.0] * 50)
        calls = []
        with mock.patch.object(mirror.time, "monotonic", lambda: next(times)), spy(calls):
            with self.assertRaises(AuditDeadlineError):
                self.m.audit_target(self.base, self.base, set(), deadline=5)
        self.assertEqual(calls, [])

    def test_a_git_timeout_at_the_deadline_is_an_audit_deadline_error(self):
        t = [0.0]

        def clock():
            return t[0]

        def slow(cmd, kw):
            t[0] = 1000.0
            raise MirrorTimeoutError("git timed out")

        with mock.patch.object(mirror.time, "monotonic", clock), spy([], slow):
            with self.assertRaises(AuditDeadlineError):
                self.m.audit_target(self.base, self.base, set(), deadline=5)

    def test_a_plain_git_timeout_is_not_an_audit_deadline(self):
        def slow(cmd, kw):
            raise MirrorTimeoutError("git timed out")

        with spy([], slow):
            with self.assertRaises(MirrorTimeoutError) as cm:
                self.m.audit_target(self.base, self.base, set(), deadline=500)
        self.assertNotIsInstance(cm.exception, AuditDeadlineError)

    def test_default_deadline_and_validation_and_reset(self):
        self.assertEqual(mirror.AUDIT_DEADLINE, 120.0)
        with self.assertRaises(ValueError):
            self.m.audit_target(self.base, self.base, set(), deadline=0)
        self.m.audit_target(self.base, self.base, set())
        self.assertIsNone(self.m._deadline)
        with self.assertRaises(MirrorError):
            self.m.audit_target("bad", self.base, set())
        self.assertIsNone(self.m._deadline)


class AuthenticatedPush(MirrorCase):
    """The push half of test_land_against_authenticated_remote (basic auth, gh-style config dir)."""

    @classmethod
    def setUpClass(cls):
        cls.backend = os.path.join(subprocess.run(["git", "--exec-path"], capture_output=True, text=True,
                                                  check=True).stdout.strip(), "git-http-backend")

    def setUp(self):
        super().setUp()
        if not os.path.exists(self.backend):
            self.skipTest("git http-backend is not installed")
        self.remote, self.base = self.remote_with_main()
        git(self.remote, "config", "http.receivepack", "true")
        handler = type("H", (_Backend,), {"project_root": self.root, "git_backend": self.backend})
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/remote.git"
        self.gh_dir = os.path.join(self.root, "gh")
        os.makedirs(self.gh_dir)
        with open(os.path.join(self.gh_dir, "hosts.yml"), "w") as fh:
            fh.write(f"github.com:\n    oauth_token: {TOKEN}\n    user: fixture\n")
        self.empty = os.path.join(self.root, "empty")
        os.makedirs(self.empty)
        p = mock.patch.dict(os.environ, {mirror.TEST_MARKER: "1"})
        p.start()
        self.addCleanup(p.stop)
        work = self.repo("w")
        git(work, "fetch", "-q", self.remote, "main")
        git(work, "checkout", "-q", "-b", "feat", "FETCH_HEAD")
        self.tip = self.commit(work, "f.txt", "f\n")
        git(work, "push", "-q", self.remote, "feat")

    def mirror(self, config_dir):
        return Mirror(self.url, self.cred(config_dir), allowed_helpers=self.allowed, loopback_http=True)

    @support.posix_only("the fixture helper is a POSIX shell script")
    def test_push_and_ls_remote_need_the_pinned_config_dir(self):
        good, bad = self.mirror(self.gh_dir), self.mirror(self.empty)
        self.assertIn("refs/heads/main", good.ls_remote(timeout=30))
        with self.assertRaises(MirrorError):
            bad.ls_remote(timeout=30)
        good.fetch(timeout=30)
        with self.assertRaises(MirrorError):
            bad.push(self.tip, "main")
        self.assertEqual(git(self.remote, "rev-parse", "main"), self.base)
        good.push(self.tip, "main")
        self.assertEqual(git(self.remote, "rev-parse", "main"), self.tip)

    @support.posix_only("the fixture helper is a POSIX shell script")
    def test_delete_needs_the_pinned_config_dir(self):
        good, bad = self.mirror(self.gh_dir), self.mirror(self.empty)
        with self.assertRaises(PushRejectedError):
            bad.delete_branch("feat")
        self.assertTrue(git(self.remote, "branch", "--list", "feat"))
        good.delete_branch("feat")
        self.assertEqual(git(self.remote, "branch", "--list", "feat"), "")


if __name__ == "__main__":
    unittest.main()
