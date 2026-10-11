"""Supervisor-private bare mirrors for the drive driver's verification (spec §3.8, §4.0 [G2], [G2a],
[F13], [F24]).

Repository config in an agent's worktree (core.sshCommand, core.fsmonitor, hooks, include.path)
can run code, so nothing the driver decides is read from a repository an agent can write. Each
destination gets one bare mirror under the supervisor's private directory
(`mirrors/<sha256(url)>.git`, reached through supervisor.privfs). Every git call runs with:

- no system or global config (GIT_CONFIG_NOSYSTEM=1, GIT_CONFIG_GLOBAL=/dev/null), HOME pointed at
  the private directory, the service PATH, and no prompts (GIT_TERMINAL_PROMPT=0);
- hooks off (core.hooksPath=/dev/null) and protocols allowed by name only (https, ssh, file);
- a timeout per call of at most MAX_CALL_TIMEOUT seconds.

Network calls (fetch) also carry the destination's pinned credential source, and nothing else from
the user's environment: a pinned helper with its config location, a pinned ssh key, or a pinned
ssh-agent socket. Those reach only that one git invocation, never sessions or other commands.
Local calls (rev-list, log, patch-id, merge-base) never see a credential.

Trust: these checks decide from git data and pinned settings, not from agent input. They do not
defeat a same-user agent that writes the private directory (spec §4.0)."""
from __future__ import annotations

import hashlib
import os
import re
import shlex
import subprocess

from swarm.supervisor import privfs
from swarm.supervisor.settings import private_dir
from swarm.supervisor.systemd import service_path

# The only credential helper a pinned source may name by default ([drive] credential_helpers).
ALLOWED_HELPERS = ("gh auth git-credential",)
MAX_CALL_TIMEOUT = 60.0

_SAFE_CONFIG = (
    "core.hooksPath=/dev/null",
    "protocol.allow=never",
    "protocol.https.allow=always",
    "protocol.ssh.allow=always",
    "protocol.file.allow=always",
)
_SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")
_LOOPBACK_HTTP = ("http://127.0.0.1:", "http://127.0.0.1/", "http://localhost:", "http://localhost/")


class MirrorError(Exception):
    """A mirror refused a request, or git failed, timed out or could not be started."""


def _sha(value: str) -> str:
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise MirrorError(f"not a commit sha: {value!r}")
    return value


def _timeout(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not value > 0:
        raise ValueError("timeout must be a positive number of seconds")
    return min(float(value), MAX_CALL_TIMEOUT)


def _abs_path(value, what: str) -> str:
    if not isinstance(value, str) or not os.path.isabs(value) or any(c in value for c in "\n\0\r"):
        raise MirrorError(f"{what} must be an absolute path: {value!r}")
    return value


def _pin_credential(credential, allowed: tuple[str, ...]):
    """The git arguments and the call environment for one pinned credential source."""
    if not isinstance(credential, dict):
        raise MirrorError("a credential source must be a mapping with a 'kind'")
    kind = credential.get("kind")
    if kind == "helper":
        command = credential.get("command")
        if not isinstance(command, str) or command not in allowed:
            raise MirrorError(f"credential helper is not on the allowlist: {command!r}")
        config_dir = _abs_path(credential.get("config_dir"), "config_dir")
        where = shlex.quote(config_dir)
        # The helper alone sees its config location, as a shell prefix of its own command line.
        helper = f"!GH_CONFIG_DIR={where} HOME={where} {command}"
        args = ["-c", "credential.helper=", "-c", "credential.useHttpPath=true",
                "-c", f"credential.helper={helper}"]
        return args, {}
    if kind == "ssh-key":
        path = _abs_path(credential.get("path"), "ssh key path")
        ssh = f"ssh -i {shlex.quote(path)} -o IdentitiesOnly=yes -o BatchMode=yes"
        return ["-c", "credential.helper="], {"GIT_SSH_COMMAND": ssh}
    if kind == "ssh-agent":
        socket = _abs_path(credential.get("socket"), "ssh-agent socket")
        return ["-c", "credential.helper="], {"SSH_AUTH_SOCK": socket}
    raise MirrorError(f"unknown credential kind: {kind!r}")


class Mirror:
    """One destination's private bare mirror. `url` is the pinned remote; `credential` is its one
    pinned source (see _pin_credential). `loopback_http` is for tests only: it allows plain http
    to 127.0.0.1 or localhost, and nothing else."""

    def __init__(self, url: str, credential: dict, *, allowed_helpers=None,
                 loopback_http: bool = False):
        if not isinstance(url, str) or not url or url.startswith("-") or any(
                c in url for c in "\n\r\0 "):
            raise MirrorError("the remote url must be a non-empty string without spaces")
        if loopback_http and not url.startswith(_LOOPBACK_HTTP):
            raise MirrorError("loopback_http only applies to http:// on 127.0.0.1 or localhost")
        allowed = ALLOWED_HELPERS if allowed_helpers is None else tuple(allowed_helpers)
        self.url = url
        self._cred_args, self._cred_env = _pin_credential(credential, allowed)
        self._proto_args = ["-c", "protocol.http.allow=always"] if loopback_http else []
        self.private_home = str(private_dir())
        with privfs.dir_fd("mirrors"):
            pass
        digest = hashlib.sha256(url.encode()).hexdigest()
        self.path = private_dir() / "mirrors" / f"{digest}.git"
        if os.path.islink(self.path):
            raise MirrorError(f"refusing a symlinked mirror: {self.path}")
        if not (self.path / "HEAD").exists():
            os.makedirs(self.path, mode=0o700, exist_ok=True)
            self._exec(["git", "init", "--bare", "--quiet", "--template=", str(self.path)],
                       env=self._env(pinned=False), timeout=MAX_CALL_TIMEOUT)

    # --- running git -------------------------------------------------------------------------

    def _env(self, pinned: bool) -> dict:
        env = {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "HOME": self.private_home,
            "PATH": service_path(),
            "GIT_TERMINAL_PROMPT": "0",
        }
        if pinned:
            env.update(self._cred_env)
        return env

    def _exec(self, cmd: list, *, env: dict, timeout: float, stdin: bytes | None = None,
              cwd: str | None = None):
        try:
            return subprocess.run(cmd, env=env, input=stdin, capture_output=True,
                                  timeout=timeout, cwd=cwd)
        except subprocess.TimeoutExpired as exc:
            raise MirrorError(f"git timed out after {timeout:g}s") from exc
        except OSError as exc:
            raise MirrorError(f"cannot run git: {exc.__class__.__name__}") from exc

    def _git(self, args: list, *, timeout: float = MAX_CALL_TIMEOUT, pinned: bool = False,
             stdin: bytes | None = None, check: bool = True):
        cmd = ["git", "--git-dir", str(self.path)]
        for item in _SAFE_CONFIG:
            cmd += ["-c", item]
        cmd += self._proto_args
        if pinned:
            cmd += self._cred_args
        cmd += args
        proc = self._exec(cmd, env=self._env(pinned), timeout=min(timeout, MAX_CALL_TIMEOUT),
                          stdin=stdin, cwd=str(self.path))
        if check and proc.returncode != 0:
            raise MirrorError(f"git {args[0]} failed: {self._last_line(proc)}")
        return proc

    def _last_line(self, proc) -> str:
        lines = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        text = lines[-1] if lines else f"exit status {proc.returncode}"
        return text.replace(self.url, "<remote>")

    def _revs(self, *args: str) -> list[str]:
        out = self._git(["rev-list", *args]).stdout.decode().split()
        return out

    def _patch_map(self, commits: list[str]) -> dict[str, str]:
        """commit sha -> `git patch-id --stable` id, for the non-merge commits among `commits`."""
        if not commits:
            return {}
        log = self._git(["log", "-p", "--no-walk=unsorted", "--no-merges", "--no-color",
                         "--no-ext-diff", "--no-textconv", *commits])
        ids = self._git(["patch-id", "--stable"], stdin=log.stdout).stdout.decode()
        mapping = {}
        for line in ids.splitlines():
            parts = line.split()
            if len(parts) == 2:
                mapping[parts[1]] = parts[0]
        return mapping

    # --- public API --------------------------------------------------------------------------

    def fetch(self, timeout: float) -> dict[str, str]:
        """Mirror the remote's branches (pruning deleted ones); returns refname -> sha."""
        seconds = _timeout(timeout)
        self._git(["fetch", "--prune", "--no-tags", "--no-write-fetch-head", "--quiet",
                   "--no-recurse-submodules", self.url, "+refs/heads/*:refs/heads/*"],
                  timeout=seconds, pinned=True)
        out = self._git(["for-each-ref", "--format=%(refname) %(objectname)", "refs/heads/"])
        refs = {}
        for line in out.stdout.decode().splitlines():
            name, _, sha = line.partition(" ")
            if name and sha:
                refs[name] = sha
        return refs

    def head(self, branch: str) -> str | None:
        """The sha of a mirrored branch, or None when the mirror has no such branch."""
        if not isinstance(branch, str) or not _BRANCH.fullmatch(branch) or ".." in branch \
                or branch.endswith("/"):
            raise MirrorError(f"not a branch name: {branch!r}")
        proc = self._git(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{commit}}"],
                         check=False)
        if proc.returncode == 1:
            return None
        if proc.returncode != 0:
            raise MirrorError(f"git rev-parse failed: {self._last_line(proc)}")
        return proc.stdout.decode().strip()

    def is_ancestor(self, a: str, b: str) -> bool:
        _sha(a)
        _sha(b)
        proc = self._git(["merge-base", "--is-ancestor", a, b], check=False)
        if proc.returncode == 0:
            return True
        if proc.returncode == 1:
            return False
        raise MirrorError(f"git merge-base failed: {self._last_line(proc)}")

    def patch_ids(self, base: str, head: str) -> set[str]:
        """The `git patch-id --stable` ids of the non-merge commits in base..head ([F13])."""
        _sha(base)
        _sha(head)
        commits = self._revs("--no-merges", f"{base}..{head}")
        return set(self._patch_map(commits).values())

    def audit_target(self, since_sha: str, candidate: str, judged: set[str]) -> list[str]:
        """Commits on the target between since_sha and candidate that are not reachable from a
        judged SHA and have no patch-id match among the judged commits ([F24]). Merges count as
        unmatched. An empty list means the target is clean. The caller passes the list through."""
        _sha(since_sha)
        _sha(candidate)
        judged_list = sorted(_sha(j) for j in judged)
        unreached = self._revs("--reverse", f"{since_sha}..{candidate}", "--not", *judged_list)
        if not unreached:
            return []
        judged_pids: set[str] = set()
        if judged_list:
            judged_commits = self._revs("--no-merges", *judged_list, "--not", since_sha)
            judged_pids = set(self._patch_map(judged_commits).values())
        pids = self._patch_map(unreached)
        return [c for c in unreached if pids.get(c) not in judged_pids]
