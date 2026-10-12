"""swarm.gitpin: the shared pinned-remote git hygiene (Task 6a's activation, Task 11b's mirror and landing).
Each function is tested on its own, including the bypasses the round-3/4 reviews tried against check_url."""
from __future__ import annotations

import os
import re
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import SANDBOX, posix_only  # noqa: F401

from swarm import gitpin  # noqa: E402

ACCEPTED = (
    "https://example.com/o/r.git",
    "https://example.com",
    "HTTPS://example.com/o/r.git",
    "https://example.com:8443/o/r.git",
    "ssh://example.com/o/r.git",
    "ssh://git@example.com/o/r.git",
    "ssh://git@example.com:2222/o/r.git",
    "ssh://[::1]/r",
    "git@example.com:o/r.git",
    "example.com:o/r.git",
    "file:///srv/repo.git",
    "/srv/repo.git",
)

# (url, the reason it is refused). The reviewers' ~50 bypass attempts, grouped by rule.
REFUSED = (
    # an option, plain or disguised
    ("--upload-pack=touch${IFS}x", "start with '-'"),
    ("--upload-pack=sh", "start with '-'"),
    ("-u", "start with '-'"),
    ("-", "start with '-'"),
    ("－－upload-pack=x", "local remote must be an absolute path"),        # fullwidth hyphen: not a URL
    ("-oProxyCommand=x:path", "start with '-'"),
    ("git@-oProxyCommand=x:path", "host that is not an option"),
    ("-oProxyCommand=x@host:path", "start with '-'"),
    ("ssh://-oProxyCommand=x/r", "host that is not an option"),
    ("ssh://-oProxyCommand=x@host/r", "host that is not an option"),
    ("ssh://%2DoProxyCommand=x/r", "host that is not an option"),
    ("ssh://%2doProxyCommand=x/r", "host that is not an option"),
    ("ssh://%252DoProxyCommand=x/r", "host that is not an option"),
    ("ssh://git@%2Dx/r", "host that is not an option"),
    ("ssh://%2Dx@host/r", "host that is not an option"),
    ("https://%2Dexample.com/r", "host that is not an option"),
    ("git@%2Dhost:o/r", "host that is not an option"),
    ("%2Duser@host:o/r", "host that is not an option"),
    ("git@host:-x", "start a segment with '-'"),
    ("git@host:o/-x", "start a segment with '-'"),
    ("git@host:%2Dx", "start a segment with '-'"),
    ("ssh://host/-x", "start a segment with '-'"),
    ("ssh://host/%2Dx", "start a segment with '-'"),
    ("ssh://host/%2dx", "start a segment with '-'"),
    ("https://host/o/%252Dx", "start a segment with '-'"),
    ("file:///srv/-r.git", "start a segment with '-'"),
    # transport helpers and other transports
    ("ext::sh -c touch% x", "whitespace or control"),
    ("ext::sh", "transport helper"),
    ("EXT::sh", "transport helper"),
    ("fd::3", "transport helper"),
    ("fd::3,4/x", "transport helper"),
    ("persistent-https::x", "transport helper"),
    ("http://example.com/r.git", "not http transport"),
    ("git://example.com/r.git", "not git transport"),
    ("ftp://example.com/r.git", "not ftp transport"),
    ("ext://example.com/r.git", "not ext transport"),
    ("://example.com/r.git", "not no transport"),
    # whitespace and controls
    ("https://exa mple.com/r.git", "whitespace or control"),
    ("https://example.com/r\n.git", "whitespace or control"),
    ("https://example.com/r\t.git", "whitespace or control"),
    ("https://example.com/r\x00", "whitespace or control"),
    ("https://example.com/r\x7f", "whitespace or control"),
    ("https://example.com/r ", "whitespace or control"),
    ("git@host:o/r.git\r", "whitespace or control"),
    # credentials, backslashes, ports
    ("https://user:TOKEN@example.com/r.git", "credentials"),
    ("https://TOKEN@example.com/r.git", "credentials"),
    ("ssh://git:pw@example.com/r.git", "credentials"),
    ("ssh://h\\@evil/r", "backslash"),
    ("https://example.com\\@evil/r", "credentials"),   # urlsplit sees userinfo first; refused either way
    ("ssh://host:99999/r", "port must be 1-65535"),
    ("ssh://host:65536/r", "port must be 1-65535"),
    ("ssh://host:0/r", "port must be 1-65535"),
    ("https://host:abc/r", "port must be 1-65535"),
    # local paths
    ("relative/path", "absolute path"),
    ("../up.git", "absolute path"),
    ("~/repo.git", "absolute path"),
    ("~user/repo.git", "absolute path"),
    ("file://host/srv/r.git", "absolute local path"),
    ("file://relative", "absolute local path"),
    # local paths with traversal, encoded or not (git decodes a file:// path)
    ("file:///srv/%2e%2e/etc/repo", "'%'"),
    ("file:///srv/%2E%2E/%2E%2E/repo", "'%'"),
    ("file:///srv/.%2e/repo", "'%'"),
    ("file:///srv/%252e%252e/repo", "'%'"),
    ("/srv/%2e%2e/repo", "'%'"),
    ("file:///srv/repo%00", "'%'"),
    ("file:///srv/../etc/repo", "'..' segment"),
    ("/srv/../etc/repo", "'..' segment"),
    ("/srv/repo/..", "'..' segment"),
    # malformed
    ("ssh://[::1/r", "not a valid URL"),
    ("[::1]:path", "absolute path"),
    ("", "non-empty"),
    ("x" * (gitpin.URL_MAX + 1), "non-empty"),
)


class CheckUrlTests(unittest.TestCase):
    def test_accepted(self):
        for url in ACCEPTED:
            with self.subTest(url=url):
                self.assertEqual(gitpin.check_url(url), url)

    def test_refused_with_the_reason(self):
        self.assertGreaterEqual(len(REFUSED), 50)
        for url, why in REFUSED:
            with self.subTest(url=url), self.assertRaisesRegex(gitpin.GitPinError, re.escape(why)):
                gitpin.check_url(url)

    def test_not_a_string_refused(self):
        for value in (None, 1, b"https://example.com/r", ["https://example.com/r"]):
            with self.subTest(value=value), self.assertRaises(gitpin.GitPinError):
                gitpin.check_url(value)

    def test_refused_is_a_value_error(self):
        with self.assertRaises(ValueError):
            gitpin.check_url("-x")

    def test_url_args_put_double_dash_first_and_check(self):
        self.assertEqual(gitpin.url_args("https://example.com/r", "refs/heads/main"),
                         ["--", "https://example.com/r", "refs/heads/main"])
        with self.assertRaises(gitpin.GitPinError):
            gitpin.url_args("--upload-pack=x")


def git(*args, cwd=None, env=None):
    base = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
    return subprocess.run(["git", *args], capture_output=True, text=True, cwd=cwd, env={**base, **(env or {})},
                          timeout=60)


class LocalityTests(unittest.TestCase):
    """The 11b re-review's local-transport probes, adapted: R1 (percent-encoded escape), R2 (locality decided
    from the URL for every credential kind), R3 (the destination's config runs in a local pack)."""

    def setUp(self):
        self.base = Path(os.path.realpath(tempfile.mkdtemp(dir=SANDBOX, prefix="gitpin-local-")))
        self.private = self.base / "private"
        self.private.mkdir()
        self.outside = self.base / "outside.git"
        git("init", "-q", "--bare", "-b", "main", str(self.outside))

    def escape_url(self) -> str:
        """file://<private>/%2e%2e/<target>: realpath of the undecoded path stays under <private>; git decodes it."""
        return "file://" + str(self.private) + "/%2e%2e/" + self.outside.name

    def test_r1_percent_encoded_escape_is_refused(self):
        url = self.escape_url()
        self.assertTrue(os.path.realpath(urllib_path(url)).startswith(str(self.private)))   # the old check passed it
        for call in (gitpin.check_url, gitpin.is_local, gitpin.resolve_local, lambda u: gitpin.locality(u, str(self.private))):
            with self.subTest(call=call), self.assertRaisesRegex(gitpin.GitPinError, "'%'"):
                call(url)

    def test_r1_plain_traversal_is_refused_and_a_symlink_resolves(self):
        with self.assertRaisesRegex(gitpin.GitPinError, "'..'"):
            gitpin.locality(str(self.private) + "/../" + self.outside.name, str(self.private))
        link = self.private / "link.git"
        link.symlink_to(self.outside)
        self.assertEqual(gitpin.resolve_local(str(link)), str(self.outside))
        self.assertEqual(gitpin.locality(str(link), str(self.private)), (True, False))
        self.assertEqual(gitpin.locality("file://" + str(link), str(self.private)), (True, False))

    def test_r2_locality_is_decided_from_the_url_alone(self):
        """A local path is local whatever credential kind is pinned (the local transport ignores an ssh pin):
        the helper takes no pin at all, and the private-dir rule is the caller's for every kind."""
        inside = self.private / "r.git"
        git("init", "-q", "--bare", str(inside))
        self.assertEqual(gitpin.locality(str(self.outside), str(self.private)), (True, False))
        self.assertEqual(gitpin.locality("file://" + str(self.outside), str(self.private)), (True, False))
        self.assertEqual(gitpin.locality(str(inside), str(self.private)), (True, True))
        self.assertEqual(gitpin.locality("file://" + str(inside), str(self.private)), (True, True))
        self.assertEqual(gitpin.locality(str(inside)), (True, False))
        root_link = self.base / "private-link"
        root_link.symlink_to(self.private)
        self.assertEqual(gitpin.locality(str(inside), str(root_link)), (True, True))
        self.assertEqual(gitpin.locality(str(self.outside), str(root_link)), (True, False))
        self.assertEqual(gitpin.locality(str(self.private) + "-sibling/r.git", str(self.private)), (True, False))
        for url in ("https://example.com/r.git", "ssh://git@example.com/r.git", "git@example.com:o/r.git"):
            with self.subTest(url=url):
                self.assertEqual(gitpin.locality(url, str(self.private)), (False, False))
                self.assertFalse(gitpin.is_local(url))
                with self.assertRaisesRegex(gitpin.GitPinError, "not a local remote"):
                    gitpin.resolve_local(url)

    def source(self):
        src = self.base / "src"
        git("init", "-q", "-b", "main", str(src))
        git("-C", str(src), "commit", "-q", "--allow-empty", "-m", "one")
        return src

    def push(self, src, dest, *pack):
        argv = gitpin.git_argv("git", "-C", str(src), "push", "-q", *pack,
                               *gitpin.url_args(str(dest), "HEAD:refs/heads/main"))
        return subprocess.run(argv, capture_output=True, text=True, timeout=60, cwd=gitpin.NEUTRAL_CWD,
                              env=gitpin.git_env(home=str(self.private)))

    def plant_alternate_refs_command(self, dest, marker):
        alt = self.base / "alt.git"
        git("init", "-q", "--bare", str(alt))
        (dest / "objects" / "info").mkdir(parents=True, exist_ok=True)
        (dest / "objects" / "info" / "alternates").write_text(str(alt / "objects") + "\n")
        git("-C", str(dest), "config", "core.alternateRefsCommand", f"sh -c 'echo ran >> {marker}; true'")

    def test_r3_alternate_refs_command_does_not_run_in_a_local_push(self):
        src = self.source()
        marker = self.base / "ALT-RAN"
        self.plant_alternate_refs_command(self.outside, marker)
        control = self.push(src, self.outside, "--receive-pack=git -c core.hooksPath=/dev/null receive-pack")
        self.assertEqual(control.returncode, 0, control.stderr)
        self.assertTrue(marker.exists(), "the probe must work without local_pack_args")      # the hole is real
        marker.unlink()
        git("-C", str(self.outside), "update-ref", "-d", "refs/heads/main")
        res = self.push(src, self.outside, *gitpin.local_pack_args("push"))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertFalse(marker.exists())

    def test_r3_hooks_do_not_run_in_a_local_push(self):
        src = self.source()
        marker = self.base / "HOOK-RAN"
        hook = self.outside / "hooks" / "pre-receive"
        hook.parent.mkdir(exist_ok=True)
        hook.write_text(f"#!/bin/sh\necho ran >> {marker}\n")
        hook.chmod(0o755)
        res = self.push(src, self.outside, *gitpin.local_pack_args("push"))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertFalse(marker.exists())

    def test_r3_update_instead_into_a_worktree_is_refused_and_fsmonitor_does_not_run(self):
        src = self.source()
        dest = self.base / "nonbare"
        git("init", "-q", "-b", "main", str(dest))
        git("-C", str(dest), "commit", "-q", "--allow-empty", "-m", "base")
        marker = self.base / "FSMON-RAN"
        git("-C", str(dest), "config", "receive.denyCurrentBranch", "updateInstead")
        git("-C", str(dest), "config", "core.fsmonitor", f"sh -c 'echo ran >> {marker}; true'")
        git("-C", str(src), "fetch", "-q", str(dest), "main")
        git("-C", str(src), "reset", "-q", "--hard", "FETCH_HEAD")
        git("-C", str(src), "commit", "-q", "--allow-empty", "-m", "two")
        res = self.push(src, dest, *gitpin.local_pack_args("push"))
        self.assertNotEqual(res.returncode, 0)                 # denyCurrentBranch=refuse wins over the repo's config
        self.assertIn("checked out", res.stderr)
        self.assertFalse(marker.exists())
        control = self.push(src, dest, "--receive-pack=git -c core.hooksPath=/dev/null receive-pack")
        self.assertEqual(control.returncode, 0, control.stderr)
        self.assertTrue(marker.exists(), "the probe must work without local_pack_args")      # the hole is real

    def test_r3_local_fetch_with_upload_pack_args_still_reads_heads(self):
        src = self.source()
        self.assertEqual(self.push(src, self.outside, *gitpin.local_pack_args("push")).returncode, 0)
        argv = gitpin.git_argv("git", "ls-remote", "--heads", gitpin.local_pack_args("ls-remote")[0],
                               *gitpin.url_args(str(self.outside)))
        res = subprocess.run(argv, capture_output=True, text=True, timeout=60, cwd=gitpin.NEUTRAL_CWD,
                             env=gitpin.git_env(home=str(self.private)))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("refs/heads/main", res.stdout)

    def test_local_pack_args_shape(self):
        (up,) = gitpin.local_pack_args("fetch", "/usr/bin/git")
        (recv,) = gitpin.local_pack_args("push", "/usr/bin/git")
        self.assertEqual(gitpin.local_pack_args("clone", "/usr/bin/git"), [up])
        self.assertEqual(gitpin.local_pack_args("ls-remote", "/usr/bin/git"), [up])
        with self.assertRaises(gitpin.GitPinError):
            gitpin.local_pack_args("archive")
        self.assertTrue(up.startswith("--upload-pack=/usr/bin/git ") and up.endswith(" upload-pack"))
        self.assertTrue(recv.startswith("--receive-pack=/usr/bin/git ") and recv.endswith(" receive-pack"))
        for item in ("core.hooksPath=" + os.devnull, "core.fsmonitor=false", "core.alternateRefsCommand=",
                     "receive.denyCurrentBranch=refuse"):
            with self.subTest(item=item):
                self.assertIn(item, gitpin.LOCAL_PACK_CONFIG)
                self.assertIn("-c " + item, up)
                self.assertIn("-c " + item, recv)


def urllib_path(url):
    import urllib.parse
    return urllib.parse.urlsplit(url).path


class CheckBranchTests(unittest.TestCase):
    def test_branches(self):
        for good in ("main", "release/1.2", "feat/x_y-z", "v1.0"):
            with self.subTest(good=good):
                self.assertEqual(gitpin.check_branch(good), good)
        for bad in ("-x", "a..b", "a b", "a\nb", "/x", "x/", "x.", "x.lock", "a//b", "", None, 1, "x" * 201,
                    ".hidden", "a:b", "a~1", "a^", "a?", "a*", "a[b", "a\\b"):
            with self.subTest(bad=bad), self.assertRaises(gitpin.GitPinError):
                gitpin.check_branch(bad)


class CheckPinTests(unittest.TestCase):
    def test_allowlisted_pins(self):
        for pin in ("helper:/usr/bin/gh auth git-credential", "helper:/opt/git-credential-manager",
                    "helper:/usr/lib/git-core/git-credential-store", "helper:/usr/lib/git-core/git-credential-cache",
                    "ssh-key:/home/u/.ssh/id", "ssh-agent:/run/user/1000/agent.sock"):
            with self.subTest(pin=pin):
                self.assertEqual(gitpin.check_pin(pin), pin)

    def test_refused_pins(self):
        for pin in ("helper:/usr/bin/curl", "helper:gh auth git-credential", "helper:/usr/bin/gh",
                    "helper:/usr/bin/gh auth login", "helper:/usr/bin/gh auth git-credential --x",
                    "helper:/usr/bin/gh  auth git-credential", "helper:/opt/git-credential-store extra",
                    "ssh-key:id", "ssh-key:/home/u/my key", "ssh-agent:sock", "ssh-agent", "nonsense",
                    "token:abc", "", None, 3, "helper:/usr/bin/gh auth git-credential\n", "ssh-agent:/run/s\x01", "ssh-key:/home/u/id\x00",
                    "ssh-key:/home/u/id\x7f", "x" * (gitpin.URL_MAX + 1)):
            with self.subTest(pin=pin), self.assertRaises(gitpin.GitPinError):
                gitpin.check_pin(pin)


class EnvAndArgvTests(unittest.TestCase):
    def test_git_env_pins_config_and_protocols(self):
        with mock.patch.dict(os.environ, {"GIT_DIR": "/evil", "GIT_CONFIG_PARAMETERS": "'x.y'='z'",
                                          "GH_TOKEN": "t", "PATH": "/p"}):
            env = gitpin.git_env(home="/h", extra={"SSH_AUTH_SOCK": "/s"})
        self.assertEqual(env, {"PATH": "/p", "HOME": "/h", "LC_ALL": "C", "GIT_TERMINAL_PROMPT": "0",
                               "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                               "GIT_ALLOW_PROTOCOL": "https:ssh:file", "SSH_AUTH_SOCK": "/s"})
        self.assertEqual(gitpin.git_env(home="/h", path="/sys")["PATH"], "/sys")

    def test_git_argv_carries_safe_config_before_the_command(self):
        argv = gitpin.git_argv("/usr/bin/git", "ls-remote")
        self.assertEqual(argv[0], "/usr/bin/git")
        self.assertEqual(argv[-1], "ls-remote")
        pairs = argv[1:-1]
        self.assertEqual(pairs[::2], ["-c"] * (len(pairs) // 2))
        self.assertEqual(set(pairs[1::2]), {"core.hooksPath=" + os.devnull, "core.fsmonitor=false",
                                            "protocol.allow=never", "protocol.https.allow=always",
                                            "protocol.ssh.allow=always", "protocol.file.allow=always"})

    def test_neutral_cwd_is_the_root(self):
        self.assertEqual(gitpin.NEUTRAL_CWD, os.path.abspath(os.sep))

    def test_ext_is_refused_by_git_under_the_pinned_env(self):
        """The env alone refuses ext:: even if a URL got past check_url and config allowed it."""
        marker = Path(tempfile.mkdtemp(dir=SANDBOX)) / "pwned"
        argv = gitpin.git_argv("git", "-c", "protocol.ext.allow=always", "ls-remote", "--",
                               f"ext::sh -c touch% {marker}")
        res = subprocess.run(argv, capture_output=True, text=True, timeout=30,
                             env=gitpin.git_env(home=str(marker.parent)), cwd=gitpin.NEUTRAL_CWD)
        self.assertNotEqual(res.returncode, 0)
        self.assertFalse(marker.exists())


class DirProblemTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(dir=SANDBOX, prefix="gitpin-"))

    def mk(self, name, mode=0o700):
        d = self.base / name
        d.mkdir()
        os.chmod(d, mode)
        return str(d)

    def test_private_dir_has_no_problem(self):
        self.assertIsNone(gitpin.dir_problem(self.mk("gh")))

    def test_relative_or_missing_or_file(self):
        self.assertRegex(gitpin.dir_problem("rel/gh"), "absolute")
        self.assertRegex(gitpin.dir_problem(None), "absolute")
        self.assertRegex(gitpin.dir_problem(str(self.base / "nope")), "does not exist")
        f = self.base / "file"
        f.write_text("")
        self.assertRegex(gitpin.dir_problem(str(f)), "not a directory")

    def test_under_a_forbidden_root_also_through_a_symlink(self):
        root = self.mk("root")
        inner = Path(root) / "gh"
        inner.mkdir(mode=0o700)
        self.assertRegex(gitpin.dir_problem(str(inner), [root]), "work root")
        self.assertRegex(gitpin.dir_problem(root, [root]), "work root")
        link = self.base / "link"
        link.symlink_to(inner)
        self.assertRegex(gitpin.dir_problem(str(link), [root]), "work root")
        self.assertIsNone(gitpin.dir_problem(self.mk("root-sibling"), [root]))

    @posix_only("needs POSIX file modes and uids")
    def test_group_or_world_writable(self):
        self.assertRegex(gitpin.dir_problem(self.mk("g", 0o770)), "group or others")
        self.assertRegex(gitpin.dir_problem(self.mk("o", 0o703)), "group or others")
        self.assertIsNone(gitpin.dir_problem(self.mk("r", 0o755)))

    @posix_only("needs POSIX file modes and uids")
    def test_parent_chain(self):
        shared = self.mk("shared", 0o777)
        os.mkdir(os.path.join(shared, "gh"), 0o700)
        self.assertRegex(gitpin.dir_problem(os.path.join(shared, "gh")), "parent")
        sticky = self.mk("sticky", 0o1777)
        os.mkdir(os.path.join(sticky, "gh"), 0o700)
        self.assertIsNone(gitpin.dir_problem(os.path.join(sticky, "gh")))

    @posix_only("needs POSIX file modes and uids")
    def test_owner_of_dir_and_parent(self):
        d = self.mk("gh")
        real_stat = os.stat

        def as_other(target):
            def fake(p, *a, **k):
                st = real_stat(p, *a, **k)
                if p == target:
                    vals = list(st)
                    vals[stat.ST_UID] = os.getuid() + 1
                    return os.stat_result(vals)
                return st
            return fake
        with mock.patch.object(gitpin.os, "stat", side_effect=as_other(os.path.realpath(d))):
            self.assertRegex(gitpin.dir_problem(d), "not owned")
        with mock.patch.object(gitpin.os, "stat", side_effect=as_other(os.path.realpath(str(self.base)))):
            self.assertRegex(gitpin.dir_problem(d), "parent")


if __name__ == "__main__":
    unittest.main()
