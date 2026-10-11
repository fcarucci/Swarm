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
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import support  # noqa: F401  (puts lib/ on sys.path, sets up the suite sandbox)
from swarm.supervisor import mirror
from swarm.supervisor.mirror import (Mirror, MirrorError, MissingCommitError, TargetRewoundError,
                                     UnpatchableCommitError)

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
BASE_ENV_KEYS = {"GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL", "HOME", "PATH", "GIT_TERMINAL_PROMPT"}
FAKE_SHA = "1" * 40
# Written out literally: a mutant that drops an entry from mirror._SAFE_CONFIG must still fail.
EXPECTED_SAFE_CONFIG = ("core.hooksPath=/dev/null", "protocol.allow=never",
                        "protocol.https.allow=always", "protocol.ssh.allow=always",
                        "protocol.file.allow=always")


def _git_env() -> dict:
    env = dict(os.environ)
    env.update({
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0",
        "GIT_AUTHOR_NAME": "fixture", "GIT_AUTHOR_EMAIL": "fixture@example.test",
        "GIT_COMMITTER_NAME": "fixture", "GIT_COMMITTER_EMAIL": "fixture@example.test",
    })
    return env


def git(cwd, *args) -> str:
    out = subprocess.run(["git", *args], cwd=cwd, env=_git_env(), capture_output=True, text=True,
                         check=True)
    return out.stdout.strip()


def spy(calls: list, fake=None):
    """Record every git invocation (argv and kwargs) made through Mirror._exec. With `fake`, the
    real git is not run: fake(cmd, kwargs) returns the CompletedProcess."""
    original = mirror.Mirror._exec

    def wrapper(self, cmd, **kw):
        calls.append((list(cmd), kw))
        if fake is not None:
            return fake(cmd, kw)
        return original(self, cmd, **kw)

    return mock.patch.object(mirror.Mirror, "_exec", wrapper)


def ok(cmd, kw):
    return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().split()[2] != "Z"
    except OSError:
        return False


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
        head, sep, payload = out.partition(b"\r\n\r\n")
        if not sep:
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

    def agent_cred(self) -> dict:
        return {"kind": "ssh-agent", "socket": SOCK}


class MirrorBasics(MirrorCase):
    def test_mirror_path_is_private_and_named_by_url_digest(self):
        import hashlib
        remote, _ = self.remote_with_main()
        m = Mirror(remote, self.agent_cred())
        digest = hashlib.sha256(remote.encode()).hexdigest()
        self.assertEqual(os.path.basename(str(m.path)), digest + ".git")
        self.assertIn(os.path.join("share", "swarm", "supervisor", "mirrors"), str(m.path))
        self.assertTrue(os.path.isdir(m.path))

    def test_fetch_head_and_is_ancestor(self):
        remote, first = self.remote_with_main()
        work = self.repo("clone")
        git(work, "fetch", "-q", remote, "main")
        git(work, "reset", "-q", "--hard", "FETCH_HEAD")
        second = self.commit(work, "x.txt", "1\n")
        git(work, "push", "-q", remote, "main")
        m = Mirror(remote, self.agent_cred())
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
        m = Mirror(remote, self.agent_cred())
        self.assertIsNotNone(m.fetch(timeout=30).get("refs/heads/topic"))
        git(remote, "update-ref", "-d", "refs/heads/topic")
        self.assertIsNone(m.fetch(timeout=30).get("refs/heads/topic"))
        self.assertIsNone(m.head("topic"))

    def test_plain_http_refused_without_loopback_opt_in(self):
        m = Mirror("http://127.0.0.1:9/none.git", self.agent_cred())
        with self.assertRaises(MirrorError):
            m.fetch(timeout=10)

    def test_loopback_http_refused_outside_the_suite(self):
        env = {k: v for k, v in os.environ.items() if k != mirror.TEST_MARKER}
        with mock.patch.dict(os.environ, env, clear=True), self.assertRaises(MirrorError):
            Mirror("http://127.0.0.1:9/x.git", self.agent_cred(), loopback_http=True)

    def test_loopback_http_hostname_must_really_be_loopback(self):
        bad = ["http://127.0.0.1:1@evil.example/x.git", "http://localhost:@evil.example/x.git",
               "http://user@127.0.0.1/x.git", "http://example.com/x.git",
               "https://127.0.0.1/x.git"]
        with mock.patch.dict(os.environ, {mirror.TEST_MARKER: "1"}):
            for url in bad:
                with self.subTest(url=url), self.assertRaises(MirrorError):
                    Mirror(url, self.agent_cred(), loopback_http=True)
            Mirror("http://127.0.0.1:9/x.git", self.agent_cred(), loopback_http=True)
            Mirror("http://localhost:9/x.git", self.agent_cred(), loopback_http=True)

    def test_loopback_flag_is_keyword_only(self):
        with self.assertRaises(TypeError):
            Mirror("http://127.0.0.1:9/x.git", self.agent_cred(), True)

    def test_url_with_embedded_credentials_refused(self):
        for url in ("https://x-access-token:FAKE@example.com/r.git", "https://user@example.com/r.git",
                    "user:FAKE@example.com:r.git"):
            with self.subTest(url=url), self.assertRaises(MirrorError):
                Mirror(url, self.agent_cred())
        Mirror("ssh://git@example.com/r.git", self.agent_cred())
        Mirror("git@example.com:r.git", self.agent_cred())

    def test_timeout_must_be_positive_and_is_capped_at_60s(self):
        remote, _ = self.remote_with_main()
        m = Mirror(remote, self.agent_cred())
        with self.assertRaises(ValueError):
            m.fetch(timeout=0)
        calls = []
        with spy(calls):
            m.fetch(timeout=500)
        self.assertTrue(calls)
        self.assertTrue(all(kw["timeout"] <= 60 for _, kw in calls))
        self.assertTrue(any(kw["timeout"] == 60 for _, kw in calls))

    def test_git_error_message_does_not_echo_the_remote_url(self):
        url = "file://" + os.path.join(self.root, "nowhere") + "?token=SECRET-ISH"
        m = Mirror(url, self.agent_cred())
        with self.assertRaises(MirrorError) as cm:
            m.fetch(timeout=30)
        self.assertNotIn("SECRET-ISH", str(cm.exception))

    @support.posix_only("symlinks")
    def test_symlinked_mirror_path_refused(self):
        remote, _ = self.remote_with_main()
        m = Mirror(remote, self.agent_cred())
        target = os.path.join(self.root, "elsewhere.git")
        shutil.rmtree(m.path)
        os.makedirs(target)
        os.symlink(target, m.path)
        with self.assertRaises(MirrorError):
            Mirror(remote, self.agent_cred())

    def test_url_starting_with_a_dash_refused(self):
        with self.assertRaises(MirrorError):
            Mirror("--upload-pack=sh", self.agent_cred())

    @support.posix_only("symlinks")
    def test_symlinked_known_hosts_refused(self):
        remote, _ = self.remote_with_main()
        m = Mirror(remote, self.agent_cred())
        os.remove(m._known_hosts)
        os.symlink(os.path.join(self.root, "elsewhere"), m._known_hosts)
        with self.assertRaises(MirrorError):
            Mirror(remote, self.agent_cred())

    def test_branch_with_dotdot_refused_before_any_git_call(self):
        remote, _ = self.remote_with_main()
        m = Mirror(remote, self.agent_cred())
        calls = []
        with spy(calls), self.assertRaises(MirrorError):
            m.head("main..evil")
        self.assertEqual(calls, [])

    def test_git_and_ssh_run_by_absolute_path_with_a_system_path(self):
        remote, _ = self.remote_with_main()
        m = Mirror(remote, self.agent_cred())
        calls = []
        with spy(calls):
            m.head("main")
        cmd, kw = calls[-1]
        self.assertTrue(os.path.isabs(cmd[0]))
        self.assertNotIn(".local", kw["env"]["PATH"])
        if os.name == "posix":
            self.assertEqual(kw["env"]["PATH"], "/usr/local/bin:/usr/bin:/bin")

    @support.posix_only("process groups")
    def test_timeout_kills_the_whole_process_group(self):
        if not sys.platform.startswith("linux"):
            self.skipTest("checks /proc")
        remote, _ = self.remote_with_main()
        m = Mirror(remote, self.agent_cred())
        pidfile = os.path.join(self.root, "grandchild.pid")
        started = time.time()
        with self.assertRaises(MirrorError):
            m._exec(["/bin/sh", "-c", f"sleep 60 & echo $! > {pidfile}; sleep 60"],
                    env=m._env(pinned=False), timeout=1)
        # A grandchild that kept the pipe open would make the call wait out its sleep.
        self.assertLess(time.time() - started, 20)
        pid = int(open(pidfile).read().strip())
        deadline = time.time() + 5
        while time.time() < deadline and _alive(pid):
            time.sleep(0.05)
        self.assertFalse(_alive(pid))


class MirrorIsolation(MirrorCase):
    def test_mirror_ignores_repo_config(self):
        # The agent's work tree (the process cwd) and the mirror's private HOME both carry
        # config that would redirect the fetch or run a command. Neither may be read.
        source, _ = self.remote_with_main()
        work = self.repo("agent")
        self.commit(work, "a.txt", "a\n")
        git(work, "config", "core.sshCommand", "touch PWNED")
        git(work, "config", f"url.file://{self.root}/nowhere.insteadOf", source)
        m = Mirror(source, self.agent_cred())
        # The mirror's HOME is the private directory: a ~/.gitconfig planted there must not apply.
        with open(os.path.join(m.private_home, ".gitconfig"), "w") as fh:
            fh.write(f'[url "file://{self.root}/nowhere"]\n\tinsteadOf = {source}\n')
        cwd = os.getcwd()
        os.chdir(work)
        self.addCleanup(os.chdir, cwd)
        sha = m.fetch(timeout=30)["refs/heads/main"]
        self.assertTrue(sha)
        self.assertFalse(os.path.exists(os.path.join(work, "PWNED")))

    def test_every_git_call_has_the_exact_sanitized_environment(self):
        remote, _ = self.remote_with_main()
        m = Mirror(remote, self.agent_cred())
        calls = []
        with spy(calls):
            m.fetch(timeout=30)
            m.head("main")
        self.assertTrue(calls)
        for cmd, kw in calls:
            env = kw["env"]
            fetch = "fetch" in cmd
            expected = BASE_ENV_KEYS | ({"SSH_AUTH_SOCK", "GIT_SSH_COMMAND"} if fetch else set())
            self.assertEqual(set(env), expected, cmd)
            self.assertEqual(env["GIT_CONFIG_NOSYSTEM"], "1")
            self.assertEqual(env["GIT_CONFIG_GLOBAL"], os.devnull)
            self.assertEqual(env["HOME"], m.private_home)
            self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
            self.assertEqual(env["PATH"], mirror._system_path())
            for item in EXPECTED_SAFE_CONFIG:
                self.assertTrue(any(cmd[i] == "-c" and cmd[i + 1] == item
                                    for i in range(len(cmd) - 1)), item)
            self.assertIn("timeout", kw)

    def test_credential_helper_not_on_allowlist_refused(self):
        remote, _ = self.remote_with_main()
        for command in ("curl evil", f"sh {self.helper}; touch PWNED", f"sh {self.helper} --x"):
            with self.subTest(command=command):
                with self.assertRaises(MirrorError) as cm:
                    Mirror(remote, {"kind": "helper", "command": command, "config_dir": self.home},
                           allowed_helpers=self.allowed)
                self.assertIn("allowlist", str(cm.exception))
        Mirror(remote, {"kind": "helper", "command": self.allowed[0], "config_dir": self.home},
               allowed_helpers=self.allowed)

    def test_default_allowlist_is_gh_git_credential_only(self):
        remote, _ = self.remote_with_main()
        with self.assertRaises(MirrorError) as cm:
            Mirror(remote, {"kind": "helper", "command": "sh /bin/true", "config_dir": self.home})
        self.assertIn("allowlist", str(cm.exception))
        try:
            Mirror(remote, {"kind": "helper", "command": "gh auth git-credential",
                            "config_dir": self.home})
        except MirrorError as exc:  # gh may live only outside the system directories
            self.assertNotIn("allowlist", str(exc))

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
        patch_marker = mock.patch.dict(os.environ, {mirror.TEST_MARKER: "1"})
        patch_marker.start()
        self.addCleanup(patch_marker.stop)

    @support.posix_only("the fixture helper is a POSIX shell script")
    def test_fetch_against_authenticated_remote_needs_pinned_config_dir(self):
        good = Mirror(self.url, self.cred(self.gh_dir), allowed_helpers=self.allowed,
                      loopback_http=True)
        self.assertEqual(good.fetch(timeout=30)["refs/heads/main"], self.sha)
        other = Mirror(self.url, self.cred(self.empty_dir), allowed_helpers=self.allowed,
                       loopback_http=True)
        with self.assertRaises(MirrorError):
            other.fetch(timeout=30)

    @support.posix_only("the fixture helper is a POSIX shell script")
    def test_pinned_config_dir_and_http_path_reach_only_the_fetch(self):
        m = Mirror(self.url, self.cred(self.gh_dir), allowed_helpers=self.allowed,
                   loopback_http=True)
        calls = []
        with spy(calls):
            m.fetch(timeout=30)
            m.head("main")
        fetch_args = [" ".join(cmd) for cmd, _ in calls if "fetch" in cmd]
        self.assertTrue(fetch_args)
        self.assertTrue(any(f"credential.helper=!GH_CONFIG_DIR={self.gh_dir}" in a
                            for a in fetch_args))
        self.assertTrue(all("credential.useHttpPath=true" in a for a in fetch_args))
        for cmd, kw in calls:
            self.assertNotIn(self.gh_dir, kw["env"].values())
            if "fetch" not in cmd:
                self.assertNotIn("credential.helper=!", " ".join(cmd))

    @support.posix_only("the fixture helper is a POSIX shell script")
    def test_helper_runs_by_absolute_path(self):
        m = Mirror(self.url, self.cred(self.gh_dir), allowed_helpers=self.allowed,
                   loopback_http=True)
        helper = [a for a in m._cred_args if a.startswith("credential.helper=!")][0]
        words = shlex.split(helper[len("credential.helper=!"):])
        self.assertTrue(os.path.isabs(words[2]), words)
        self.assertEqual(words[3], self.helper)


class SshPinning(MirrorCase):
    def test_ssh_agent_socket_pinned_and_passed_only_to_git(self):
        remote, _ = self.remote_with_main()
        m = Mirror(remote, self.agent_cred())
        calls = []
        with mock.patch.dict(os.environ, {"SSH_AUTH_SOCK": SOCK_OTHER}), spy(calls, ok):
            m.fetch(timeout=30)
            m.head("main")
        fetch_env = [kw["env"] for cmd, kw in calls if "fetch" in cmd][0]
        head_env = [kw["env"] for cmd, kw in calls if "rev-parse" in cmd][0]
        self.assertEqual(fetch_env["SSH_AUTH_SOCK"], SOCK)
        self.assertNotIn("SSH_AUTH_SOCK", head_env)
        self.assertIn(f"IdentityAgent={SOCK}", fetch_env["GIT_SSH_COMMAND"])

    def test_ssh_key_pinned_as_git_ssh_command_only(self):
        remote, _ = self.remote_with_main()
        key = os.path.join(self.root, "id_fixture")
        m = Mirror(remote, {"kind": "ssh-key", "path": key})
        calls = []
        with spy(calls, ok):
            m.fetch(timeout=30)
            m.head("main")
        fetch_env = [kw["env"] for cmd, kw in calls if "fetch" in cmd][0]
        head_env = [kw["env"] for cmd, kw in calls if "rev-parse" in cmd][0]
        ssh = fetch_env["GIT_SSH_COMMAND"]
        for needle in (f"-i {shlex.quote(key)}", "IdentitiesOnly=yes", "IdentityAgent=none",
                       "BatchMode=yes", "-F", shlex.quote(os.devnull)):
            self.assertIn(needle, ssh)
        self.assertNotIn("SSH_AUTH_SOCK", fetch_env)
        self.assertNotIn("GIT_SSH_COMMAND", head_env)

    def test_every_ssh_kind_is_batch_mode_and_pins_known_hosts(self):
        remote, _ = self.remote_with_main()
        creds = [{"kind": "ssh-key", "path": os.path.join(self.root, "k")},
                 {"kind": "ssh-agent", "socket": SOCK},
                 {"kind": "helper", "command": f"sh {self.helper}", "config_dir": self.home}]
        for cred in creds:
            with self.subTest(kind=cred["kind"]):
                m = Mirror(remote, cred, allowed_helpers=self.allowed)
                ssh = m._cred_env["GIT_SSH_COMMAND"]
                self.assertIn("BatchMode=yes", ssh)
                self.assertIn(f"UserKnownHostsFile={shlex.quote(m._known_hosts)}", ssh)
                self.assertIn(os.path.join("mirrors", "known_hosts"), m._known_hosts)


class PatchIdsAndAudit(MirrorCase):
    def setUp(self):
        super().setUp()
        self.remote = self.repo("remote.git", bare=True)
        self.work = self.repo("work")
        self.base = self.commit(self.work, "base.txt", "base\n", "base")
        git(self.work, "push", "-q", self.remote, "main")
        self.mirror = Mirror(self.remote, self.agent_cred())

    def push(self, *refspecs):
        git(self.work, "push", "-q", "-f", self.remote, *refspecs)
        self.mirror.fetch(timeout=30)

    def test_patch_ids_equal_on_clean_rebase(self):
        git(self.work, "checkout", "-q", "-b", "feat")
        judged = self.commit(self.work, "feat.txt", "feature\n", "feat")
        git(self.work, "checkout", "-q", "main")
        self.commit(self.work, "other.txt", "moved on\n", "main moves")
        self.push("main", "feat")
        new_base = git(self.work, "rev-parse", "HEAD")
        git(self.work, "checkout", "-q", "-B", "feat-rebased", "main")
        git(self.work, "cherry-pick", judged)
        rebased = git(self.work, "rev-parse", "HEAD")
        self.push("feat-rebased")
        self.assertNotEqual(judged, rebased)
        self.assertEqual(self.mirror.patch_ids(self.base, judged),
                         self.mirror.patch_ids(new_base, rebased))
        self.assertEqual(len(self.mirror.patch_ids(self.base, judged)), 1)

    def test_multi_commit_range_patch_ids_survive_a_moved_base(self):
        git(self.work, "checkout", "-q", "-b", "feat")
        self.commit(self.work, "one.txt", "1\n", "one")
        c2 = self.commit(self.work, "two.txt", "2\n", "two")
        git(self.work, "checkout", "-q", "main")
        new_base = self.commit(self.work, "other.txt", "moved on\n", "main moves")
        git(self.work, "checkout", "-q", "-B", "feat-rebased", "main")
        git(self.work, "cherry-pick", f"{self.base}..{c2}")
        rebased = git(self.work, "rev-parse", "HEAD")
        self.push("main", "feat", "feat-rebased")
        before = self.mirror.patch_ids(self.base, c2)
        after = self.mirror.patch_ids(new_base, rebased)
        self.assertEqual(len(before), 2)
        self.assertEqual(before, after)

    def test_rebase_changing_content_changes_patch_ids(self):
        git(self.work, "checkout", "-q", "-b", "feat")
        judged = self.commit(self.work, "feat.txt", "feature\n", "feat")
        git(self.work, "checkout", "-q", "-B", "edited", "main")
        edited = self.commit(self.work, "feat.txt", "feature, edited\n", "edited")
        self.push("feat", "edited")
        self.assertNotEqual(self.mirror.patch_ids(self.base, judged),
                            self.mirror.patch_ids(self.base, edited))

    def test_whitespace_only_twin_is_flagged(self):
        # --stable strips whitespace, so `rm -rf /tmp/build` and `rm -rf / tmp/build` would match.
        git(self.work, "checkout", "-q", "-b", "feat")
        judged = self.commit(self.work, "build.sh", "rm -rf /tmp/build\n", "judged")
        git(self.work, "checkout", "-q", "-B", "twin", "main")
        twin = self.commit(self.work, "build.sh", "rm -rf / tmp/build\n", "twin")
        self.push("feat", "twin")
        self.assertNotEqual(self.mirror.patch_ids(self.base, judged),
                            self.mirror.patch_ids(self.base, twin))
        self.assertEqual(self.mirror.audit_target(self.base, twin, {judged}), [twin])

    def test_unjudged_commit_on_target_is_flagged(self):
        judged = self.commit(self.work, "j.txt", "judged\n", "judged")
        self.push("main")
        unjudged = self.commit(self.work, "u.txt", "sneaked in\n", "unjudged")
        self.push("main")
        self.assertEqual(self.mirror.audit_target(self.base, unjudged, {judged}), [unjudged])
        self.assertEqual(self.mirror.audit_target(self.base, judged, {judged}), [])
        self.assertEqual(self.mirror.audit_target(self.base, unjudged, {unjudged}), [])

    def test_cherry_picked_judged_commit_is_not_flagged(self):
        judged = self.commit(self.work, "j.txt", "judged\n", "judged")
        self.push("main")
        git(self.work, "checkout", "-q", "-B", "twin", self.base)
        twin = self.commit(self.work, "j.txt", "judged\n", "same change, other sha")
        self.assertNotEqual(twin, judged)
        self.push("twin:refs/heads/main")
        self.assertEqual(self.mirror.audit_target(self.base, twin, {judged}), [])

    def test_revert_and_reapply_is_flagged(self):
        # M1: a judged change that was judged, reverted and judged again must not justify a
        # re-applied copy of itself that lands after the revert.
        j1 = self.commit(self.work, "config.txt", "auth=off\n", "judged: auth off")
        git(self.work, "revert", "--no-edit", j1)
        j2 = git(self.work, "rev-parse", "HEAD")
        git(self.work, "cherry-pick", j1)
        evil = git(self.work, "rev-parse", "HEAD")
        self.push("main")
        self.assertEqual(self.mirror.audit_target(self.base, evil, {j1, j2}), [evil])
        self.assertEqual(self.mirror.audit_target(self.base, j2, {j1, j2}), [])

    def test_judged_merge_is_not_flagged(self):
        # A judged merge in the judged history: its parents are reachable from it, so they pass.
        git(self.work, "checkout", "-q", "-b", "side", self.base)
        self.commit(self.work, "side.txt", "side\n", "side")
        git(self.work, "checkout", "-q", "main")
        self.commit(self.work, "main.txt", "main\n", "main")
        git(self.work, "merge", "-q", "--no-ff", "-m", "judged merge", "side")
        merge = git(self.work, "rev-parse", "HEAD")
        self.push("main", "side")
        self.assertEqual(self.mirror.audit_target(self.base, merge, {merge}), [])

    def test_unjudged_merge_on_target_is_flagged(self):
        judged = self.commit(self.work, "j.txt", "judged\n", "judged")
        git(self.work, "checkout", "-q", "-b", "side", self.base)
        self.commit(self.work, "side.txt", "side\n", "side")
        git(self.work, "checkout", "-q", "main")
        git(self.work, "merge", "-q", "--no-ff", "-m", "unjudged merge", "side")
        merge = git(self.work, "rev-parse", "HEAD")
        self.push("main", "side")
        self.assertIn(merge, self.mirror.audit_target(self.base, merge, {judged}))

    def test_unjudged_empty_commit_on_target_is_flagged(self):
        judged = self.commit(self.work, "j.txt", "judged\n", "judged")
        git(self.work, "commit", "-q", "--allow-empty", "-m", "empty")
        empty = git(self.work, "rev-parse", "HEAD")
        self.push("main")
        self.assertEqual(self.mirror.audit_target(self.base, empty, {judged}), [empty])

    def test_patch_ids_refuses_a_range_with_a_merge_or_empty_commit(self):
        git(self.work, "checkout", "-q", "-b", "side", self.base)
        self.commit(self.work, "side.txt", "side\n", "side")
        git(self.work, "checkout", "-q", "main")
        self.commit(self.work, "main.txt", "main\n", "main")
        git(self.work, "merge", "-q", "--no-ff", "-m", "merge", "side")
        merged = git(self.work, "rev-parse", "HEAD")
        self.push("main", "side")
        with self.assertRaises(UnpatchableCommitError):
            self.mirror.patch_ids(self.base, merged)
        git(self.work, "commit", "-q", "--allow-empty", "-m", "empty")
        empty = git(self.work, "rev-parse", "HEAD")
        self.push("main")
        with self.assertRaises(UnpatchableCommitError):
            self.mirror.patch_ids(self.base, empty)

    def test_judged_sha_missing_from_the_mirror_raises_typed_error(self):
        self.push("main")
        with self.assertRaises(MissingCommitError):
            self.mirror.audit_target(self.base, self.base, {FAKE_SHA})
        with self.assertRaises(MissingCommitError):
            self.mirror.patch_ids(self.base, FAKE_SHA)

    def test_rewound_target_refused(self):
        j = self.commit(self.work, "j.txt", "judged\n", "judged")
        self.push("main")
        with self.assertRaises(TargetRewoundError):
            self.mirror.audit_target(j, self.base, set())

    def test_shas_go_on_stdin_not_argv(self):
        j = self.commit(self.work, "j.txt", "judged\n", "judged")
        u = self.commit(self.work, "u.txt", "u\n", "u")
        self.push("main")
        calls = []
        with spy(calls):
            self.mirror.audit_target(self.base, u, {j})
        shas = {self.base, j, u}
        self.assertTrue(calls)
        for cmd, _ in calls:
            if "merge-base" in cmd:  # is_ancestor takes exactly two shas, on argv by design
                continue
            self.assertFalse(shas & set(cmd), cmd)


if __name__ == "__main__":
    unittest.main()
