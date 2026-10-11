"""Task 11a: supervisor-private mirrors (swarm.supervisor.mirror).

Every repository here is a local one under the suite sandbox; HOME is a temporary directory inside
the sandbox, so the mirror's private directory and any planted ~/.gitconfig live there. Credentials
are fake: a test token in a gh-style hosts.yml, read by a fixture helper that honours GH_CONFIG_DIR."""
from __future__ import annotations

import base64
import http.server
import os
import shlex
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

import support  # noqa: F401  (puts lib/ on sys.path, sets up the suite sandbox)
from swarm.supervisor import mirror
from swarm.supervisor.mirror import Mirror, MirrorError
from swarm.supervisor.systemd import service_path

# POSIX-looking absolute paths, made absolute on Windows too (support.abs_).
SOCK = support.abs_("/run/agent.sock")
SOCK_OTHER = support.abs_("/run/user/1000/agent.sock")
TOKEN = "test-token-not-a-secret"
TOKEN_USER = "x-access-token"
HELPER_SH = """#!/bin/sh
# Fixture credential helper: honours GH_CONFIG_DIR like gh does, fails without a token there.
[ "$1" = get ] || exit 0
tok=$(sed -n 's/^ *oauth_token: *//p' "$GH_CONFIG_DIR/hosts.yml" 2>/dev/null)
[ -n "$tok" ] || exit 1
printf 'username=%s\\npassword=%s\\n' "{user}" "$tok"
"""


def _git_env(**extra):
    env = dict(os.environ)
    env.update({
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0",
        "GIT_AUTHOR_NAME": "fixture", "GIT_AUTHOR_EMAIL": "fixture@example.test",
        "GIT_COMMITTER_NAME": "fixture", "GIT_COMMITTER_EMAIL": "fixture@example.test",
    })
    env.update(extra)
    return env


def git(cwd, *args) -> str:
    out = subprocess.run(["git", *args], cwd=cwd, env=_git_env(), capture_output=True, text=True,
                         check=True)
    return out.stdout.strip()


class _Backend(http.server.BaseHTTPRequestHandler):
    """A stdlib HTTP front for `git http-backend` that requires basic auth with TOKEN."""
    project_root = ""
    git_backend = ""

    def log_message(self, *args):  # keep test output quiet
        pass

    def do_GET(self):
        self._cgi()

    def do_POST(self):
        self._cgi()

    def _cgi(self):
        want = "Basic " + base64.b64encode(f"{TOKEN_USER}:{TOKEN}".encode()).decode()
        if self.headers.get("Authorization") != want:
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="git"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        path, _, query = self.path.partition("?")
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        env = {
            "PATH": os.environ.get("PATH", ""), "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_PROJECT_ROOT": self.project_root,
            "GIT_HTTP_EXPORT_ALL": "1", "PATH_INFO": path, "QUERY_STRING": query,
            "REQUEST_METHOD": self.command, "CONTENT_TYPE": self.headers.get("Content-Type", ""),
            "CONTENT_LENGTH": str(len(body)), "REMOTE_ADDR": "127.0.0.1", "REMOTE_USER": TOKEN_USER,
        }
        out = subprocess.run([self.git_backend], input=body, env=env, capture_output=True).stdout
        head, _, payload = out.partition(b"\r\n\r\n")
        if not _:
            head, _, payload = out.partition(b"\n\n")
        status = 200
        headers = []
        for line in head.decode("latin-1").splitlines():
            if line.lower().startswith("status:"):
                status = int(line.split(":", 1)[1].strip().split()[0])
            elif ":" in line:
                headers.append(tuple(line.split(":", 1)))
        self.send_response(status)
        for k, v in headers:
            self.send_header(k, v.strip())
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class MirrorCase(unittest.TestCase):
    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="mirror-", dir=support.SANDBOX))
        self.home = os.path.join(self.root, "home")
        os.makedirs(self.home)
        patch = mock.patch.dict(os.environ, support.home_env(self.home))
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(shutil.rmtree, self.root, True)
        self.helper = os.path.join(self.root, "helper.sh")
        with open(self.helper, "w") as fh:
            fh.write(HELPER_SH.replace("{user}", TOKEN_USER))
        os.chmod(self.helper, 0o700)
        self.allowed = (f"sh {self.helper}",)

    def repo(self, name: str, bare: bool = False) -> str:
        path = os.path.join(self.root, name)
        args = ["git", "init", "-q", "-b", "main"] + (["--bare"] if bare else []) + [path]
        subprocess.run(args, env=_git_env(), check=True)
        return path

    def commit(self, path: str, fname: str, text: str, msg: str = "change") -> str:
        with open(os.path.join(path, fname), "w") as fh:
            fh.write(text)
        git(path, "add", fname)
        git(path, "commit", "-q", "-m", msg)
        return git(path, "rev-parse", "HEAD")

    def remote_with_main(self):
        """A bare remote whose main has one commit; returns (remote path, first sha)."""
        work = self.repo("seed")
        sha = self.commit(work, "base.txt", "base\n", "base")
        remote = self.repo("remote.git", bare=True)
        git(work, "push", "-q", remote, "main")
        return remote, sha

    def cred(self, config_dir: str | None = None) -> dict:
        return {"kind": "helper", "command": f"sh {self.helper}",
                "config_dir": config_dir or self.home}


class MirrorBasics(MirrorCase):
    def test_mirror_path_is_private_and_named_by_url_digest(self):
        import hashlib
        remote, _ = self.remote_with_main()
        m = Mirror(remote, {"kind": "ssh-agent", "socket": SOCK})
        digest = hashlib.sha256(remote.encode()).hexdigest()
        self.assertEqual(os.path.basename(m.path), digest + ".git")
        self.assertIn(os.path.join("share", "swarm", "supervisor", "mirrors"), str(m.path))
        self.assertTrue(os.path.isdir(m.path))

    def test_fetch_head_and_is_ancestor(self):
        remote, first = self.remote_with_main()
        work = self.repo("clone")
        git(work, "fetch", "-q", remote, "main")
        git(work, "reset", "-q", "--hard", "FETCH_HEAD")
        second = self.commit(work, "x.txt", "1\n")
        git(work, "push", "-q", remote, "main")
        m = Mirror(remote, {"kind": "ssh-agent", "socket": SOCK})
        refs = m.fetch(timeout=30)
        self.assertEqual(refs["refs/heads/main"], second)
        self.assertEqual(m.head("main"), second)
        self.assertIsNone(m.head("no-such-branch"))
        self.assertTrue(m.is_ancestor(first, second))
        self.assertFalse(m.is_ancestor(second, first))

    def test_fetch_prunes_deleted_branches(self):
        remote, _ = self.remote_with_main()
        work = self.repo("clone")
        git(work, "fetch", "-q", remote, "main")
        git(work, "push", "-q", remote, "FETCH_HEAD:refs/heads/topic")
        m = Mirror(remote, {"kind": "ssh-agent", "socket": SOCK})
        self.assertIsNotNone(m.fetch(timeout=30).get("refs/heads/topic"))
        git(remote, "update-ref", "-d", "refs/heads/topic")
        self.assertIsNone(m.fetch(timeout=30).get("refs/heads/topic"))
        self.assertIsNone(m.head("topic"))

    def test_plain_http_refused_without_loopback_opt_in(self):
        m = Mirror("http://127.0.0.1:9/none.git", {"kind": "ssh-agent", "socket": SOCK})
        with self.assertRaises(MirrorError):
            m.fetch(timeout=10)

    def test_loopback_http_only_with_explicit_opt_in(self):
        with self.assertRaises(MirrorError):
            Mirror("http://example.com/x.git", {"kind": "ssh-agent", "socket": SOCK},
                   loopback_http=True)
        Mirror("http://127.0.0.1:9/x.git", {"kind": "ssh-agent", "socket": SOCK},
               loopback_http=True)

    def test_timeout_must_be_positive_and_is_capped_at_60s(self):
        remote, _ = self.remote_with_main()
        m = Mirror(remote, {"kind": "ssh-agent", "socket": SOCK})
        with self.assertRaises(ValueError):
            m.fetch(timeout=0)
        seen = []
        real = subprocess.run

        def spy(*a, **kw):
            seen.append(kw.get("timeout"))
            return real(*a, **kw)

        with mock.patch.object(mirror.subprocess, "run", side_effect=spy):
            m.fetch(timeout=500)
        self.assertTrue(seen)
        self.assertTrue(all(t <= 60 for t in seen))

    def test_git_error_message_does_not_echo_the_remote_url(self):
        url = "file://" + os.path.join(self.root, "nowhere") + "?token=SECRET-ISH"
        m = Mirror(url, {"kind": "ssh-agent", "socket": SOCK})
        with self.assertRaises(MirrorError) as cm:
            m.fetch(timeout=30)
        self.assertNotIn("SECRET-ISH", str(cm.exception))


class MirrorIsolation(MirrorCase):
    def test_mirror_ignores_repo_config(self):
        # The agent's work tree (the process cwd) and the mirror's private HOME both carry
        # config that would redirect the fetch or run a command. Neither may be read.
        source, _ = self.remote_with_main()
        work = self.repo("agent")
        self.commit(work, "a.txt", "a\n")
        git(work, "config", "core.sshCommand", "touch PWNED")
        git(work, "config", f"url.file://{self.root}/nowhere.insteadOf", source)
        m = Mirror(source, {"kind": "ssh-agent", "socket": SOCK})
        # The mirror's HOME is the private directory: a ~/.gitconfig planted there must not apply.
        with open(os.path.join(m.private_home, ".gitconfig"), "w") as fh:
            fh.write(f'[url "file://{self.root}/nowhere"]\n\tinsteadOf = {source}\n')
        cwd = os.getcwd()
        os.chdir(work)
        self.addCleanup(os.chdir, cwd)
        sha = m.fetch(timeout=30)["refs/heads/main"]
        self.assertTrue(sha)
        self.assertFalse(os.path.exists(os.path.join(work, "PWNED")))

    def test_every_git_call_has_the_sanitized_environment(self):
        remote, _ = self.remote_with_main()
        m = Mirror(remote, {"kind": "ssh-agent", "socket": SOCK})
        calls = []
        real = subprocess.run

        def spy(args, **kw):
            calls.append((list(args), kw))
            return real(args, **kw)

        with mock.patch.object(mirror.subprocess, "run", side_effect=spy):
            m.fetch(timeout=30)
            m.head("main")
        self.assertTrue(calls)
        for args, kw in calls:
            env = kw["env"]
            self.assertEqual(env["GIT_CONFIG_NOSYSTEM"], "1")
            self.assertEqual(env["GIT_CONFIG_GLOBAL"], os.devnull)
            self.assertEqual(env["HOME"], m.private_home)
            self.assertEqual(env["PATH"], service_path())
            for needed in ("core.hooksPath=/dev/null", "protocol.allow=never",
                           "protocol.https.allow=always", "protocol.ssh.allow=always",
                           "protocol.file.allow=always"):
                self.assertIn(needed, args)
            self.assertIn("timeout", kw)

    def test_credential_helper_not_on_allowlist_refused(self):
        remote, _ = self.remote_with_main()
        for command in ("curl evil", f"sh {self.helper}; touch PWNED", f"sh {self.helper} --x"):
            with self.subTest(command=command):
                with self.assertRaises(MirrorError):
                    Mirror(remote, {"kind": "helper", "command": command, "config_dir": self.home},
                           allowed_helpers=self.allowed)
        Mirror(remote, {"kind": "helper", "command": self.allowed[0], "config_dir": self.home},
               allowed_helpers=self.allowed)

    def test_default_allowlist_is_gh_git_credential_only(self):
        remote, _ = self.remote_with_main()
        with self.assertRaises(MirrorError):
            Mirror(remote, {"kind": "helper", "command": "sh /bin/true", "config_dir": self.home})
        Mirror(remote, {"kind": "helper", "command": "gh auth git-credential",
                        "config_dir": self.home})

    def test_malformed_credential_refused(self):
        remote, _ = self.remote_with_main()
        bad = [
            {"kind": "unknown"},
            {"kind": "ssh-key", "path": "relative/key"},
            {"kind": "ssh-key", "path": "/k\ney"},
            {"kind": "ssh-agent", "socket": "agent.sock"},
            {"kind": "helper", "command": "gh auth git-credential", "config_dir": "rel"},
        ]
        for cred in bad:
            with self.subTest(cred=cred), self.assertRaises(MirrorError):
                Mirror(remote, cred)


class AuthenticatedRemote(MirrorCase):
    """The mirror half of test_land_against_authenticated_remote: a basic-auth smart-HTTP remote."""

    @classmethod
    def setUpClass(cls):
        cls.backend = subprocess.run(["git", "--exec-path"], capture_output=True, text=True,
                                     check=True).stdout.strip()
        cls.backend = os.path.join(cls.backend, "git-http-backend")

    def setUp(self):
        super().setUp()
        if not os.path.exists(self.backend):
            self.skipTest("git http-backend is not installed")
        self.remote, self.sha = self.remote_with_main()
        handler = type("H", (_Backend,), {"project_root": self.root, "git_backend": self.backend})
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/remote.git"
        # gh-style config dir with the fake token; the default HOME (private) has none.
        self.gh_dir = os.path.join(self.root, "gh")
        os.makedirs(self.gh_dir)
        with open(os.path.join(self.gh_dir, "hosts.yml"), "w") as fh:
            fh.write(f"github.com:\n    oauth_token: {TOKEN}\n    user: fixture\n")
        self.empty_dir = os.path.join(self.root, "empty-gh")
        os.makedirs(self.empty_dir)

    @support.posix_only("the fixture helper is a POSIX shell script")
    def test_fetch_against_authenticated_remote_needs_pinned_config_dir(self):
        ok = Mirror(self.url, self.cred(self.gh_dir), allowed_helpers=self.allowed,
                    loopback_http=True)
        self.assertEqual(ok.fetch(timeout=30)["refs/heads/main"], self.sha)
        other = Mirror(self.url, self.cred(self.empty_dir), allowed_helpers=self.allowed,
                       loopback_http=True)
        with self.assertRaises(MirrorError):
            other.fetch(timeout=30)

    @support.posix_only("the fixture helper is a POSIX shell script")
    def test_pinned_config_dir_reaches_only_the_helper_invocation(self):
        m = Mirror(self.url, self.cred(self.gh_dir), allowed_helpers=self.allowed,
                   loopback_http=True)
        args_seen = []
        real = subprocess.run

        def spy(args, **kw):
            args_seen.append((list(args), kw))
            return real(args, **kw)

        with mock.patch.object(mirror.subprocess, "run", side_effect=spy):
            m.fetch(timeout=30)
            m.head("main")
        for args, kw in args_seen:
            self.assertNotIn(self.gh_dir, kw["env"].values())
        fetch_args = [a for a, _ in args_seen if "fetch" in a]
        self.assertTrue(any(f"credential.helper=!GH_CONFIG_DIR={self.gh_dir}" in " ".join(a)
                            for a in fetch_args))


class SshPinning(MirrorCase):
    def test_ssh_agent_socket_pinned_and_passed_only_to_git(self):
        remote, _ = self.remote_with_main()
        m = Mirror(remote, {"kind": "ssh-agent", "socket": SOCK_OTHER})
        seen = []

        def fake(args, **kw):
            seen.append((list(args), kw["env"]))
            return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")

        with mock.patch.dict(os.environ, {"SSH_AUTH_SOCK": "/somebody/else.sock"}), \
                mock.patch.object(mirror.subprocess, "run", side_effect=fake):
            m.fetch(timeout=30)
            m.head("main")
        fetch_env = [e for a, e in seen if "fetch" in a][0]
        head_env = [e for a, e in seen if "rev-parse" in a][0]
        self.assertEqual(fetch_env["SSH_AUTH_SOCK"], SOCK_OTHER)
        self.assertNotIn("SSH_AUTH_SOCK", head_env)

    def test_ssh_key_pinned_as_git_ssh_command_only(self):
        remote, _ = self.remote_with_main()
        key = os.path.join(self.root, "id_fixture")
        m = Mirror(remote, {"kind": "ssh-key", "path": key})
        seen = []

        def fake(args, **kw):
            seen.append((list(args), kw["env"]))
            return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")

        with mock.patch.object(mirror.subprocess, "run", side_effect=fake):
            m.fetch(timeout=30)
            m.head("main")
        fetch_env = [e for a, e in seen if "fetch" in a][0]
        head_env = [e for a, e in seen if "rev-parse" in a][0]
        self.assertEqual(fetch_env["GIT_SSH_COMMAND"],
                         f"ssh -i {shlex.quote(key)} -o IdentitiesOnly=yes -o BatchMode=yes")
        self.assertNotIn("GIT_SSH_COMMAND", head_env)


class PatchIdsAndAudit(MirrorCase):
    def setUp(self):
        super().setUp()
        self.remote = self.repo("remote.git", bare=True)
        self.work = self.repo("work")
        self.base = self.commit(self.work, "base.txt", "base\n", "base")
        git(self.work, "push", "-q", self.remote, "main")
        self.mirror = Mirror(self.remote, {"kind": "ssh-agent", "socket": SOCK})

    def push_main(self):
        git(self.work, "push", "-q", self.remote, "main")
        self.mirror.fetch(timeout=30)

    def test_patch_ids_equal_on_clean_rebase(self):
        git(self.work, "checkout", "-q", "-b", "feat")
        judged = self.commit(self.work, "feat.txt", "feature\n", "feat")
        git(self.work, "checkout", "-q", "main")
        self.commit(self.work, "other.txt", "moved on\n", "main moves")
        self.push_main()
        new_base = git(self.work, "rev-parse", "HEAD")
        git(self.work, "checkout", "-q", "-B", "feat-rebased", "main")
        git(self.work, "cherry-pick", judged)
        rebased = git(self.work, "rev-parse", "HEAD")
        git(self.work, "push", "-q", self.remote, judged + ":refs/heads/feat", "feat-rebased")
        self.mirror.fetch(timeout=30)
        self.assertNotEqual(judged, rebased)
        self.assertEqual(self.mirror.patch_ids(self.base, judged),
                         self.mirror.patch_ids(new_base, rebased))
        self.assertEqual(len(self.mirror.patch_ids(self.base, judged)), 1)

    def test_rebase_changing_content_changes_patch_ids(self):
        git(self.work, "checkout", "-q", "-b", "feat")
        judged = self.commit(self.work, "feat.txt", "feature\n", "feat")
        git(self.work, "push", "-q", self.remote, "feat")
        git(self.work, "checkout", "-q", "-B", "edited", "main")
        edited = self.commit(self.work, "feat.txt", "feature, edited\n", "edited")
        git(self.work, "push", "-q", self.remote, "edited")
        self.mirror.fetch(timeout=30)
        self.assertNotEqual(self.mirror.patch_ids(self.base, judged),
                            self.mirror.patch_ids(self.base, edited))

    def test_unjudged_commit_on_target_is_flagged(self):
        judged = self.commit(self.work, "j.txt", "judged\n", "judged")
        self.push_main()
        unjudged = self.commit(self.work, "u.txt", "sneaked in\n", "unjudged")
        self.push_main()
        self.assertEqual(self.mirror.audit_target(self.base, unjudged, {judged}), [unjudged])
        self.assertEqual(self.mirror.audit_target(self.base, judged, {judged}), [])
        self.assertEqual(self.mirror.audit_target(self.base, unjudged, {unjudged}), [])

    def test_cherry_picked_judged_commit_is_not_flagged(self):
        judged = self.commit(self.work, "j.txt", "judged\n", "judged")
        self.push_main()
        git(self.work, "checkout", "-q", "-B", "twin", self.base)
        twin = self.commit(self.work, "j.txt", "judged\n", "same change, other sha")
        self.assertNotEqual(twin, judged)
        git(self.work, "checkout", "-q", "-B", "main", twin)
        git(self.work, "push", "-q", "-f", self.remote, "main")
        self.mirror.fetch(timeout=30)
        self.assertEqual(self.mirror.audit_target(self.base, twin, {judged}), [])


if __name__ == "__main__":
    unittest.main()
