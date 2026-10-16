"""Supervisor-private bare mirrors for the drive driver's verification (spec §3.8, §4.0 [G2], [G2a],
[F13], [F24]).

Repository config in an agent's worktree (core.sshCommand, core.fsmonitor, hooks, include.path)
can run code, so nothing the driver decides is read from a repository an agent can write. Each
destination gets one bare mirror under the supervisor's private directory
(`mirrors/<sha256(url)>.git`, reached through supervisor.privfs). Every git call runs with:

- the absolute git binary, from system directories only (never ~/.local/bin), and a PATH made of
  those directories;
- no system or global config (GIT_CONFIG_NOSYSTEM=1, GIT_CONFIG_GLOBAL=/dev/null), HOME pointed at
  the private directory, and no prompts (GIT_TERMINAL_PROMPT=0);
- hooks off (core.hooksPath=/dev/null) and protocols allowed by name only (https, ssh, file);
- a timeout per call of at most MAX_CALL_TIMEOUT seconds, enforced on the call's whole process
  group, so helpers and ssh children die with git.

Network calls (fetch) also carry the destination's one pinned credential source, and nothing else
from the user's environment: an allowlisted helper with its config location, an ssh key, or an
ssh-agent socket. Those reach only that one git invocation. Local calls never see a credential.
ssh runs with `-F /dev/null`, a pinned known_hosts file, and BatchMode=yes, so it never prompts.

Trust: these checks decide from git data and pinned settings, not from agent input. They do not
defeat a same-user agent that writes the private directory (spec §4.0)."""
from __future__ import annotations

import hashlib
import os
import re
import shlex
import shutil
import signal
import subprocess
import time
from collections import Counter
from urllib.parse import urlsplit

from swarm import gitpin
from swarm.supervisor import privfs
from swarm.supervisor.settings import private_dir

# The only credential helper a pinned source may name by default ([drive] credential_helpers).
ALLOWED_HELPERS = tuple(" ".join((name, *args)) for name, args in gitpin.CREDENTIAL_HELPERS.items())
MAX_CALL_TIMEOUT = 60.0
# Overall deadline of one audit_target call, on top of the per-call cap (L8): a large history
# must not hold the supervisor pass for audit_calls * 60 s.
AUDIT_DEADLINE = 120.0
KEYSCAN_TIMEOUT = 15.0
SYSTEM_DIRS = ("/usr/local/bin", "/usr/bin", "/bin")
HELPER_SYSTEM_DIRS = (*SYSTEM_DIRS, "/usr/lib/git-core", "/usr/libexec/git-core")
# The suite sets this to its sandbox; loopback_http is refused without it.
TEST_MARKER = "SWARM_TEST_SANDBOX"

_SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost")


class MirrorError(Exception):
    """A mirror refused a request, or git failed, timed out or could not be started."""


class MirrorTimeoutError(MirrorError):
    """A git call ran out of time (its process group was killed)."""


class AuditDeadlineError(MirrorTimeoutError):
    """audit_target ran past its overall deadline; the caller retries in a later pass."""


class PushRejectedError(MirrorError):
    """The destination refused a push (non-fast-forward, protected branch, hook). `.reason` is the
    remote's own last line, without the url."""

    def __init__(self, message: str, reason: str = ""):
        super().__init__(message)
        self.reason = reason


class MissingCommitError(MirrorError):
    """A SHA the caller names is not in the mirror. Callers treat this as a blocking DRIVER-AUDIT."""


class TargetRewoundError(MirrorError):
    """The target no longer descends from the activation-time head (it was reset backwards)."""


class UnpatchableCommitError(MirrorError):
    """A commit in the range is a merge or empty, so it has no patch-id to compare."""


def _system_path() -> str:
    if os.name == "nt":
        return os.environ.get("PATH", "")
    return os.pathsep.join(SYSTEM_DIRS)


def _resolve(tool: str) -> str:
    """The absolute path of a tool in the system directories (never ~/.local/bin)."""
    found = shutil.which(tool, path=_system_path())
    if not found or not os.path.isabs(found):
        raise MirrorError(f"{tool} is not installed in a system directory")
    return found


def _sha(value) -> str:
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise MirrorError(f"not a commit sha: {value!r}")
    return value


def _timeout(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not value > 0:
        raise ValueError("timeout must be a positive number of seconds")
    return float(value)


def _abs_path(value, what: str) -> str:
    if not isinstance(value, str) or not os.path.isabs(value) or any(c in value for c in "\n\0\r"):
        raise MirrorError(f"{what} must be an absolute path: {value!r}")
    return value


def _split(url: str):
    try:
        return urlsplit(url)
    except ValueError as exc:
        raise MirrorError("the remote url is malformed") from exc


def _check_url(url, loopback_http: bool) -> str:
    """The pinned URL when a mirror may use it. gitpin.check_url is the one rule (no option-like URL or
    transport helper, no credentials, https/ssh/file or an absolute path); plain http is allowed to
    loopback only, and only in the test suite."""
    if loopback_http:
        if not os.environ.get(TEST_MARKER):
            raise MirrorError("loopback_http is for the test suite only")
        if not isinstance(url, str) or not gitpin.no_controls(url) or url.startswith("-"):
            raise MirrorError("the remote url must be a non-empty string without spaces")
        parts = _split(url)
        if (parts.scheme != "http" or parts.hostname not in _LOOPBACK_HOSTS
                or "@" in parts.netloc):
            raise MirrorError("loopback_http only applies to http:// on 127.0.0.1 or localhost")
        return url
    try:
        return gitpin.check_url(url)
    except gitpin.GitPinError as exc:
        raise MirrorError(str(exc)) from exc


def _check_local(url: str) -> None:
    """A local destination is written by whoever can write its directory (the integrator pushes its branch there),
    so outside the test suite only the supervisor's private directory is trusted, whatever the credential kind (gitpin.locality
    decides from the URL alone, decoded the way git decodes it); and it must be a bare repository (a work tree would
    run config and filters on update)."""
    try:
        _, under = gitpin.locality(url, str(private_dir()))
        real = gitpin.resolve_local(url)
    except gitpin.GitPinError as exc:
        raise MirrorError(str(exc)) from exc
    if not under and not os.environ.get(TEST_MARKER):
        raise MirrorError("a local destination must lie in the supervisor's private directory")
    if os.path.exists(os.path.join(real, ".git")) or not (
            os.path.isfile(os.path.join(real, "HEAD")) and os.path.isdir(os.path.join(real, "objects"))):
        raise MirrorError("a local destination must be a bare repository")
    # The directory layout is not proof: its own config decides whether git treats it as bare.
    for key, want in (("core.bare", "true"), ("core.worktree", None)):
        proc = _tool(gitpin.git_argv(_resolve("git"), "--git-dir", real, "config", "--get", key), 10,
                     gitpin.git_env(home=str(private_dir()), path=_system_path()))
        value = proc.stdout.decode("utf-8", "replace").strip()
        if (want is None and value) or (want is not None and value not in ("", want)):
            raise MirrorError(f"a local destination must be a bare repository ({key} = {value})")


def _runs(spine: list[str], matched: set[str]) -> list[list[str]]:
    """Maximal runs of consecutive commits on the first-parent spine that are all matched."""
    runs, current = [], []
    for c in spine:
        if c in matched:
            current.append(c)
        elif current:
            runs.append(current)
            current = []
    if current:
        runs.append(current)
    return runs


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except OSError:
        try:
            proc.kill()
        except OSError:
            pass


# scp-style `user@host:path`. A one-letter "host" is a Windows drive (C:\repo), as git itself reads it.
_SCP_URL = re.compile(r"(?:[^@/:\s]+@)?(?P<host>[A-Za-z0-9][A-Za-z0-9.-]+):(?!//)")


def ssh_host_of(url: str) -> tuple[str, int] | None:
    """(host, port) of an ssh remote (`ssh://[user@]host[:port]/path` or scp-style `user@host:path`)."""
    if "://" in url:
        parts = _split(url)
        if parts.scheme != "ssh" or not parts.hostname:
            return None
        try:
            return parts.hostname, parts.port or 22
        except ValueError as exc:
            raise MirrorError("the remote url has a bad port") from exc
    m = _SCP_URL.match(url)
    return (m.group("host"), 22) if m else None


def _known_name(host: str, port: int) -> str:
    return host if port == 22 else f"[{host}]:{port}"


def known_host_present(path: str, host: str, port: int) -> bool:
    """Whether the known_hosts file at `path` has an entry for host:port (plain host names only; a
    hashed or wildcard entry written by hand is not recognised, and doctor then asks for a seed)."""
    name = _known_name(host, port)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                fields = line.split()
                if len(fields) >= 3 and not line.startswith("#") and name in fields[0].split(","):
                    return True
    except OSError:
        return False
    return False


def known_hosts_file() -> str:
    """The private known_hosts file (created empty, 0600, in the private mirrors directory). The
    only file ssh may trust host keys from; it is only appended to by add_known_host."""
    with privfs.dir_fd("mirrors"):
        pass
    path = os.path.join(str(private_dir()), "mirrors", "known_hosts")
    if os.path.islink(path):
        raise MirrorError("refusing a symlinked known_hosts file")
    if not os.path.exists(path):
        with open(path, "a"):
            pass
        os.chmod(path, 0o600)
    return path


def _check_helper_file(path: str, trusted: tuple) -> None:
    """A pinned absolute credential helper must be a regular executable that a launch cannot replace:
    in a system directory or named exactly by the person ([drive] credential_helpers paths), owned by root
    or this user, and neither the file nor any parent directory writable by group or others."""
    real = os.path.realpath(path)
    if not os.path.isfile(real) or not os.access(real, os.X_OK):
        raise MirrorError(f"credential helper {path} is missing or not executable")
    system = {os.path.realpath(d) for d in HELPER_SYSTEM_DIRS}
    named = {os.path.realpath(t) for t in trusted}
    if os.path.dirname(real) not in system and real not in named:
        raise MirrorError(f"credential helper {path} is not in a system directory or named in "
                          f"[drive] credential_helpers paths")
    if os.name == "nt":
        return
    node = real
    while True:
        st = os.stat(node)
        sticky_dir = node != real and st.st_mode & 0o1000          # /tmp-style: others cannot replace our entry
        if st.st_uid not in (0, os.getuid()) or (st.st_mode & 0o022 and not sticky_dir):
            raise MirrorError(f"credential helper {path}: {node} is writable by others or owned by another user")
        parent = os.path.dirname(node)
        if parent == node:
            break
        node = parent


def _tool(cmd: list[str], timeout: float, env: dict | None = None) -> subprocess.CompletedProcess:
    """Run a system tool (ssh-keyscan, ssh-keygen) with a minimal environment, killed with its
    process group on timeout."""
    kwargs = {"start_new_session": True} if os.name == "posix" else {}
    env = env if env is not None else {"PATH": _system_path(), "LC_ALL": "C"}
    try:
        proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                stdin=subprocess.DEVNULL, **kwargs)
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _kill_group(proc)
        proc.communicate()
        raise MirrorTimeoutError(f"{os.path.basename(cmd[0])} timed out after {timeout:g}s") from exc
    except OSError as exc:
        raise MirrorError(f"cannot run {os.path.basename(cmd[0])}: {exc.__class__.__name__}") from exc
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def scan_host_keys(host: str, port: int = 22, timeout: float = KEYSCAN_TIMEOUT) -> list[tuple[str, str]]:
    """[(known_hosts line, SHA256 fingerprint)] for the keys `host` offers, from ssh-keyscan. The
    keys are NOT trusted: they are only shown to a person, who confirms them ([G2], never
    accept-new). MirrorError when the host offers nothing."""
    if not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", host):
        raise MirrorError(f"not a host name: {host!r}")
    if not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536:
        raise MirrorError(f"not a port: {port!r}")
    scan = _tool([_resolve("ssh-keyscan"), "-T", str(int(_timeout(timeout))), "-p", str(port),
                  "-t", "ed25519,ecdsa,rsa", host], _timeout(timeout) + 5)
    lines = [ln for ln in scan.stdout.decode("utf-8", "replace").splitlines()
             if ln.strip() and not ln.startswith("#")]
    out = []
    for line in lines:
        fields = line.split()
        if len(fields) < 3:
            continue
        out.append((line, _fingerprint(fields[1], fields[2])))
    if not out:
        raise MirrorError(f"{host}:{port} offered no host keys")
    return out


def _fingerprint(keytype: str, b64: str) -> str:
    import base64
    try:
        raw = base64.b64decode(b64.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError) as exc:
        raise MirrorError("a host key is not valid base64") from exc
    digest = base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")
    return f"{keytype} SHA256:{digest}"


def add_known_host(path: str, host: str, port: int, lines: list[str], *, confirmed: bool) -> int:
    """Append the host keys a person confirmed to the private known_hosts file. `confirmed` must be
    exactly True (the caller got it from a person who saw the fingerprints): there is no
    accept-new. Every line must name this host and port and carry a valid key. Returns the number
    of lines added."""
    if confirmed is not True:
        raise MirrorError("host keys are added only when a person confirmed their fingerprints")
    if os.path.islink(path):
        raise MirrorError("refusing a symlinked known_hosts file")
    name = _known_name(host, port)
    good = []
    for line in lines:
        fields = line.split()
        if len(fields) < 3 or fields[0] != name or fields[1] not in (
                "ssh-ed25519", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521",
                "ssh-rsa") or any(c in line for c in "\n\r\0"):
            raise MirrorError(f"not a host key line for {name}")
        _fingerprint(fields[1], fields[2])
        good.append(" ".join(fields[:3]))
    with open(path, "a+", encoding="utf-8") as fh:
        fh.seek(0, os.SEEK_END)
        if fh.tell():
            fh.seek(fh.tell() - 1)
            if fh.read(1) != "\n":
                fh.write("\n")
        for line in good:
            fh.write(line + "\n")
    os.chmod(path, 0o600)
    return len(good)


class Mirror:
    """One destination's private bare mirror. `url` is the pinned remote; `credential` is its one
    pinned source. `loopback_http` (keyword-only) allows plain http to 127.0.0.1 or localhost, and
    only inside the test suite (TEST_MARKER must be set); production never passes it."""

    def __init__(self, url: str, credential: dict, *, allowed_helpers=None,
                 loopback_http: bool = False, trusted_helper_paths=()):
        self.url = _check_url(url, loopback_http)
        self._loopback = loopback_http
        self._deadline: float | None = None
        self._trusted_paths = tuple(trusted_helper_paths)
        self._local = False if loopback_http else gitpin.is_local(self.url)
        self._shown_url = self.url                # what errors hide; git is given the resolved path below
        if self._local:
            _check_local(self.url)
            # The checks ran on the resolved path: git gets exactly that path, never the URL, so a launch-writable
            # symlink swapped after the check cannot redirect a later fetch, push or audit.
            self.url = gitpin.resolve_local(self.url)
        allowed = ALLOWED_HELPERS if allowed_helpers is None else tuple(allowed_helpers)
        self._git_bin = _resolve("git")
        self._ssh_bin = _resolve("ssh")
        self.private_home = str(private_dir())
        with privfs.dir_fd("mirrors"):
            pass
        self._known_hosts = self._ensure_known_hosts()
        self._cred_args, self._cred_env = self._pin(credential, allowed)
        self._proto_args = ["-c", "protocol.http.allow=always"] if loopback_http else []
        digest = hashlib.sha256(url.encode()).hexdigest()
        self.path = private_dir() / "mirrors" / f"{digest}.git"
        if os.path.islink(self.path):
            raise MirrorError(f"refusing a symlinked mirror: {self.path}")
        if not (self.path / "HEAD").exists():
            os.makedirs(self.path, mode=0o700, exist_ok=True)
            self._exec([self._git_bin, "init", "--bare", "--quiet", "--template=", str(self.path)],
                       env=self._env(pinned=False), timeout=MAX_CALL_TIMEOUT)

    # --- pinned credential sources ---------------------------------------------------------

    def _ensure_known_hosts(self) -> str:
        return known_hosts_file()

    def _ssh_command(self, options: list[str]) -> str:
        parts = [self._ssh_bin, "-F", os.devnull, "-o", f"UserKnownHostsFile={self._known_hosts}",
                 "-o", "StrictHostKeyChecking=yes", "-o", "BatchMode=yes", *options]
        return " ".join(shlex.quote(p) for p in parts)

    def _pin(self, credential, allowed: tuple[str, ...]):
        """The git arguments and the fetch-only environment for one pinned credential source."""
        if not isinstance(credential, dict):
            raise MirrorError("a credential source must be a mapping with a 'kind'")
        kind = credential.get("kind")
        if kind == "helper":
            command = credential.get("command")
            if not isinstance(command, str) or not command:
                raise MirrorError(f"credential helper is not on the allowlist: {command!r}")
            config_dir = _abs_path(credential.get("config_dir"), "config_dir")
            first, _, rest = command.partition(" ")
            if os.path.isabs(first):
                # A pinned absolute executable ([11b] ruling a): its basename plus the fixed
                # arguments must be on the allowlist, and it must be a runnable regular file.
                named = " ".join(p for p in (os.path.basename(first), rest) if p)
                if named not in allowed:
                    raise MirrorError(f"credential helper is not on the allowlist: {command!r}")
                _check_helper_file(first, self._trusted_paths)
                absolute = command
            else:
                if command not in allowed:
                    raise MirrorError(f"credential helper is not on the allowlist: {command!r}")
                absolute = " ".join(p for p in (_resolve(first), rest) if p)
            where = shlex.quote(config_dir)
            # The helper alone sees its config location, as a shell prefix of its own command.
            exe, _, fixed = absolute.partition(" ")
            helper = f"!GH_CONFIG_DIR={where} HOME={where} " + " ".join(
                p for p in (shlex.quote(exe), fixed) if p)
            args = ["-c", "credential.helper=", "-c", "credential.useHttpPath=true",
                    "-c", f"credential.helper={helper}"]
            return args, {"GIT_SSH_COMMAND": self._ssh_command(["-o", "IdentityAgent=none"])}
        if kind == "ssh-key":
            path = _abs_path(credential.get("path"), "ssh key path")
            ssh = self._ssh_command(["-i", path, "-o", "IdentitiesOnly=yes",
                                     "-o", "IdentityAgent=none"])
            return ["-c", "credential.helper="], {"GIT_SSH_COMMAND": ssh}
        if kind == "ssh-agent":
            socket = _abs_path(credential.get("socket"), "ssh-agent socket")
            ssh = self._ssh_command(["-o", f"IdentityAgent={socket}"])
            return ["-c", "credential.helper="], {"GIT_SSH_COMMAND": ssh, "SSH_AUTH_SOCK": socket}
        if kind == "none":
            # A local destination (a path or file:// url) needs no credential; a network one does.
            if "://" in self.url and not self.url.startswith("file://") or (
                    "://" not in self.url and not os.path.isabs(self.url)):
                raise MirrorError("a network destination needs a pinned credential source")
            return ["-c", "credential.helper="], {}
        raise MirrorError(f"unknown credential kind: {kind!r}")

    # --- running git -----------------------------------------------------------------------

    def _env(self, pinned: bool) -> dict:
        extra = dict(self._cred_env) if pinned else {}
        if self._loopback:                       # test suite only: plain http to 127.0.0.1
            extra["GIT_ALLOW_PROTOCOL"] = gitpin.ALLOW_PROTOCOL + ":http"
        return gitpin.git_env(home=self.private_home, path=_system_path(), extra=extra)

    def _exec(self, cmd: list, *, env: dict, timeout: float, stdin: bytes | None = None,
              cwd: str | None = None) -> subprocess.CompletedProcess:
        kwargs = {"start_new_session": True} if os.name == "posix" else {}
        try:
            proc = subprocess.Popen(cmd, env=env, cwd=cwd, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE,
                                    stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                                    **kwargs)
        except OSError as exc:
            raise MirrorError(f"cannot run git: {exc.__class__.__name__}") from exc
        try:
            out, err = proc.communicate(stdin, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            _kill_group(proc)
            proc.communicate()
            raise MirrorTimeoutError(f"git timed out after {timeout:g}s") from exc
        return subprocess.CompletedProcess(cmd, proc.returncode, out, err)

    def _git(self, args: list, *, timeout: float = MAX_CALL_TIMEOUT, pinned: bool = False,
             stdin: bytes | None = None, check: bool = True) -> subprocess.CompletedProcess:
        cmd = gitpin.git_argv(self._git_bin, "--git-dir", str(self.path), *self._proto_args,
                              *(self._cred_args if pinned else ()), *args)
        seconds = min(float(timeout), MAX_CALL_TIMEOUT)
        if self._deadline is not None:
            left = self._deadline - time.monotonic()
            if left <= 0:
                raise AuditDeadlineError("the audit ran past its overall deadline")
            seconds = min(seconds, left)
        try:
            proc = self._exec(cmd, env=self._env(pinned), timeout=seconds, stdin=stdin,
                              cwd=gitpin.NEUTRAL_CWD)
        except MirrorTimeoutError as exc:
            if self._deadline is not None and time.monotonic() >= self._deadline:
                raise AuditDeadlineError("the audit ran past its overall deadline") from exc
            raise
        if check and proc.returncode != 0:
            raise MirrorError(f"git {args[0]} failed: {self._last_line(proc)}")
        return proc

    def _last_line(self, proc) -> str:
        lines = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        text = lines[-1] if lines else f"exit status {proc.returncode}"
        return text.replace(self._shown_url, "<remote>").replace(self.url, "<remote>")

    def _lines(self, args: list, stdin: bytes | None = None) -> list[str]:
        return self._git(args, stdin=stdin).stdout.decode().split()

    def _revs(self, positive: list[str], negative: list[str], reverse: bool = False,
              first_parent: bool = False) -> list[str]:
        """Commits reachable from `positive` and not from `negative`; shas go on stdin, not argv."""
        if not positive:
            return []
        lines = [*positive, *(f"^{n}" for n in negative)]
        args = ["rev-list", "--stdin"] + (["--reverse"] if reverse else []) + (
            ["--first-parent"] if first_parent else [])
        return self._lines(args, stdin=("\n".join(lines) + "\n").encode())

    def _require_known(self, shas) -> None:
        wanted = sorted(set(shas))
        if not wanted:
            return
        out = self._git(["cat-file", "--batch-check=%(objectname) %(objecttype)"],
                        stdin=("\n".join(wanted) + "\n").encode())
        for line in out.stdout.decode().splitlines():
            parts = line.split()
            if len(parts) != 2 or parts[1] != "commit":
                raise MissingCommitError(f"commit {parts[0] if parts else '?'} is not in the mirror")

    def _patch_map(self, commits: list[str]) -> dict[str, str]:
        """commit sha -> `git patch-id --verbatim` id. Merges and empty commits have no entry."""
        if not commits:
            return {}
        log = self._git(["log", "-p", "--no-walk=unsorted", "--no-color", "--no-ext-diff",
                         "--no-textconv", "--stdin"], stdin=("\n".join(commits) + "\n").encode())
        ids = self._git(["patch-id", "--verbatim"], stdin=log.stdout).stdout.decode()
        mapping = {}
        for line in ids.splitlines():
            parts = line.split()
            if len(parts) == 2:
                mapping[parts[1]] = parts[0]
        return mapping

    # --- public API ------------------------------------------------------------------------

    def fetch(self, timeout: float) -> dict[str, str]:
        """Mirror the remote's branches (pruning deleted ones); returns refname -> sha."""
        seconds = _timeout(timeout)
        self._git(["fetch", "--prune", "--no-tags", "--no-write-fetch-head", "--quiet",
                   "--no-recurse-submodules", *self._pack("fetch"),
                   *self._u("+refs/heads/*:refs/heads/*")],
                  timeout=seconds, pinned=True)
        out = self._git(["for-each-ref", "--format=%(refname) %(objectname)", "refs/heads/"])
        refs = {}
        for line in out.stdout.decode().splitlines():
            name, _, sha = line.partition(" ")
            if name and sha:
                refs[name] = sha
        return refs

    def fetch_branches(self, branches: list[str], timeout: float) -> tuple[dict[str, str], list[str]]:
        """Fetch the named branches one at a time inside one overall `timeout` (each call is also
        capped at MAX_CALL_TIMEOUT). Returns (refname -> sha of what arrived, branches still
        pending). A branch whose fetch times out, and every branch after it, stays pending: the
        caller keeps the list (the ledger's mirror_fetch_pending) and calls again in a later pass,
        so a big repository is brought in over several passes. Any other git failure (a branch the
        remote does not have, a refused credential) raises MirrorError."""
        total = _timeout(timeout)
        names = [self._branch(b) for b in branches]
        end = time.monotonic() + total
        got: dict[str, str] = {}
        for i, name in enumerate(names):
            left = end - time.monotonic()
            if left <= 0:
                return got, names[i:]
            try:
                self._git(["fetch", "--no-tags", "--no-write-fetch-head", "--quiet",
                           "--no-recurse-submodules", *self._pack("fetch"),
                           *self._u(f"+refs/heads/{name}:refs/heads/{name}")],
                          timeout=left, pinned=True)
            except MirrorTimeoutError:
                return got, names[i:]
            sha = self.head(name)
            if sha:
                got[f"refs/heads/{name}"] = sha
        return got, []

    def fetch_local_branch(self, path: str, branch: str, timeout: float, into: str | None = None) -> str | None:
        """Bring `branch` from the job's own work repository at `path` (launches commit there and never push: spec
        rev 7, only the supervisor lands) into this mirror; returns its sha, or None when it has no such branch.
        The path is agent-writable, so only objects come from it: git reads it through upload-pack with the safe
        local settings (gitpin.local_pack_args), never its config, hooks or filters, from a neutral cwd."""
        name = self._branch(branch)
        dest = self._branch(into or branch)
        try:
            # the plain absolute path, as pinned local destinations are given (a file:// URI of a Windows path
            # is not one gitpin accepts)
            args = gitpin.url_args(os.path.realpath(path), f"+refs/heads/{name}:refs/heads/{dest}")
        except (gitpin.GitPinError, ValueError) as exc:
            raise MirrorError(str(exc)) from exc
        proc = self._git(["fetch", "--no-tags", "--no-write-fetch-head", "--quiet", "--no-recurse-submodules",
                          *gitpin.local_pack_args("fetch", self._git_bin), *args],
                         timeout=_timeout(timeout), check=False)
        if proc.returncode != 0:
            return None
        return self.head(dest)

    def ls_remote(self, timeout: float) -> dict[str, str]:
        """The remote's branches (refname -> sha), asked with the same pinned credential and
        environment as fetch and push. Used by doctor, and to verify a landing."""
        seconds = _timeout(timeout)
        out = self._git(["ls-remote", "--heads", *self._pack("ls-remote"), *self._u()], timeout=seconds, pinned=True)
        refs = {}
        for line in out.stdout.decode("utf-8", "replace").splitlines():
            sha, _, name = line.partition("\t")
            if _SHA.fullmatch(sha) and name.startswith("refs/heads/"):
                refs[name] = sha
        return refs

    def push(self, sha: str, target: str, *, timeout: float = MAX_CALL_TIMEOUT) -> None:
        """Fast-forward `target` on the remote to exactly `sha`: `git push <url> <sha>:refs/heads/<target>`
        with the pinned credential. Never a force (no `+`, no --force); a non-fast-forward or
        protected branch is PushRejectedError. The commit must already be in the mirror."""
        _sha(sha)
        branch = self._branch(target)
        seconds = _timeout(timeout)
        self._require_known([sha])
        proc = self._git(["push", "--porcelain", *self._pack("push"), *self._u(f"{sha}:refs/heads/{branch}")],
                         timeout=seconds, pinned=True, check=False)
        if proc.returncode != 0:
            reason = self._last_line(proc)
            raise PushRejectedError(f"push of {sha[:12]} to {branch} failed: {reason}", reason)

    def delete_branch_at(self, branch: str, sha: str, *, timeout: float = MAX_CALL_TIMEOUT) -> None:
        """Delete `branch` on the remote only while it names exactly `sha` (a compare-and-delete:
        `--force-with-lease=refs/heads/<branch>:<sha>`), pinned credential. A branch that moved is left alone
        (PushRejectedError)."""
        _sha(sha)
        name = self._branch(branch)
        seconds = _timeout(timeout)
        proc = self._git(["push", "--porcelain", f"--force-with-lease=refs/heads/{name}:{sha}", *self._pack("push"),
                          *self._u(f":refs/heads/{name}")], timeout=seconds, pinned=True, check=False)
        if proc.returncode != 0:
            reason = self._last_line(proc)
            raise PushRejectedError(f"deleting {name} at {sha[:12]} failed: {reason}", reason)

    def delete_branch(self, branch: str, *, timeout: float = MAX_CALL_TIMEOUT) -> None:
        """Delete a branch on the remote (`git push <url> :refs/heads/<branch>`), pinned credential."""
        name = self._branch(branch)
        seconds = _timeout(timeout)
        proc = self._git(["push", "--porcelain", *self._pack("push"), *self._u(f":refs/heads/{name}")], timeout=seconds,
                         pinned=True, check=False)
        if proc.returncode != 0:
            reason = self._last_line(proc)
            raise PushRejectedError(f"deleting {name} failed: {reason}", reason)

    # --- ssh host keys (person-confirmed only) -----------------------------------------------

    def ssh_host(self) -> tuple[str, int] | None:
        """(host, port) when the remote is reached over ssh, else None."""
        return ssh_host_of(self.url)

    def host_key_known(self) -> bool:
        """Whether the private known_hosts file has a key for this remote's ssh host."""
        hp = self.ssh_host()
        return hp is None or known_host_present(self._known_hosts, *hp)

    def _u(self, *rest: str) -> list[str]:
        """`--`, the pinned URL, then `rest`: the URL is never readable as an option. (Loopback http, test suite
        only, was checked in __init__ and is not a gitpin transport.)"""
        if self._loopback:
            return ["--", self.url, *rest]
        try:
            return gitpin.url_args(self.url, *rest)
        except gitpin.GitPinError as exc:
            raise MirrorError(str(exc)) from exc

    def _pack(self, op: str) -> list[str]:
        """Local transports run upload-pack / receive-pack in the destination repository and git clears the -c
        configuration for them: gitpin.local_pack_args passes the safe settings again (hooks, fsmonitor and
        alternateRefsCommand off, no push into a checked-out branch)."""
        return gitpin.local_pack_args(op, self._git_bin) if self._local else []

    def _branch(self, branch) -> str:
        if not isinstance(branch, str) or not _BRANCH.fullmatch(branch) or ".." in branch \
                or branch.endswith("/") or branch.endswith(".lock"):
            raise MirrorError(f"not a branch name: {branch!r}")
        return branch

    def head(self, branch: str) -> str | None:
        """The sha of a mirrored branch, or None when the mirror has no such branch."""
        self._branch(branch)
        proc = self._git(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{commit}}"],
                         check=False)
        if proc.returncode == 1:
            return None
        if proc.returncode != 0:
            raise MirrorError(f"git rev-parse failed: {self._last_line(proc)}")
        return proc.stdout.decode().strip()

    def merge_base(self, a: str, b: str) -> str | None:
        """The best common ancestor of two mirrored commits, or None when they share none."""
        _sha(a)
        _sha(b)
        self._require_known([a, b])
        proc = self._git(["merge-base", a, b], check=False)
        if proc.returncode == 1:
            return None
        if proc.returncode != 0:
            raise MirrorError(f"git merge-base failed: {self._last_line(proc)}")
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

    def patch_ids(self, base: str, head: str) -> list[str]:
        """The sorted `patch-id --verbatim` ids of the commits in base..head (a multiset: a commit
        applied twice counts twice). A merge or empty commit in the range raises
        UnpatchableCommitError: it has no id, so it can never be proven to be a judged change."""
        _sha(base)
        _sha(head)
        self._require_known([base, head])
        commits = self._revs([head], [base])
        mapping = self._patch_map(commits)
        missing = [c for c in commits if c not in mapping]
        if missing:
            raise UnpatchableCommitError(
                f"{len(missing)} commit(s) in {base}..{head} are merges or empty, e.g. {missing[0]}")
        return sorted(mapping[c] for c in commits)

    def audit_target(self, since_sha: str, candidate: str, judged: set[str], *,
                     deadline: float | None = AUDIT_DEADLINE) -> list[str]:
        """The commits on the target between since_sha and candidate that no judged change
        accounts for ([F24]), in target order. A commit is accounted for when it is reachable from
        a judged SHA, or when its patch-id matches a judged change that is not already on the
        target. Each judged change accounts for at most one such commit, so a revert followed by a
        re-applied cherry-pick is flagged. Merges and empty commits are flagged unless judged.
        Raises MissingCommitError for a judged SHA the mirror lacks, and TargetRewoundError when
        since_sha is not an ancestor of candidate. `deadline` (seconds, default AUDIT_DEADLINE) bounds
        the whole call on top of the per-call cap: AuditDeadlineError when it runs out ([11b] L8);
        None means only the per-call cap applies."""
        if deadline is not None:
            deadline = _timeout(deadline)
        self._deadline = None if deadline is None else time.monotonic() + deadline
        try:
            return self._audit(since_sha, candidate, judged)
        finally:
            self._deadline = None

    def _audit(self, since_sha: str, candidate: str, judged: set[str]) -> list[str]:
        _sha(since_sha)
        _sha(candidate)
        judged_list = sorted({_sha(j) for j in judged})
        self._require_known([since_sha, candidate, *judged_list])
        if not self.is_ancestor(since_sha, candidate):
            raise TargetRewoundError(f"{since_sha} is not an ancestor of {candidate}")
        on_target = set(self._revs([candidate], [since_sha]))
        unreached = self._revs([candidate], [since_sha, *judged_list], reverse=True)
        if not unreached:
            return []
        judged_new = self._revs(judged_list, [since_sha])
        judged_ids = self._patch_map(judged_new)
        budget = Counter(judged_ids[c] for c in judged_new if c in judged_ids)
        for c in judged_new:
            if c in on_target and c in judged_ids:
                budget[judged_ids[c]] -= 1
        ids = self._patch_map(unreached)
        matched = set()
        for c in unreached:
            pid = ids.get(c)
            if pid is not None and budget[pid] > 0:
                budget[pid] -= 1
                matched.add(c)
        accepted = self._accepted_runs(since_sha, candidate, judged_list, matched, ids)
        return [c for c in unreached if c not in accepted]

    def _accepted_runs(self, since: str, candidate: str, judged_list: list[str], matched: set[str],
                       ids: dict[str, str]) -> set[str]:
        """The matched commits that sit in a run which is a contiguous slice of some judged
        chain, in order, and whose tree equals that judged slice merged onto the run's parent
        ([F24] M2, L7). A commit outside every accepted run is flagged, whatever its patch-id."""
        if not matched:
            return set()
        chains = []
        for j in judged_list:
            chain = self._revs([j], [since], reverse=True, first_parent=True)
            chain_ids = self._patch_map(chain)
            seq = [chain_ids.get(c) for c in chain]
            if chain and None not in seq:
                chains.append((chain, seq))
        accepted: set[str] = set()
        spine = self._revs([candidate], [since], reverse=True, first_parent=True)
        for run in _runs(spine, matched):
            pids = [ids[c] for c in run]
            ours = self._lines(["rev-parse", f"{run[0]}^1"])[0]
            if any(self._run_matches(chain, seq, pids, ours, since, run[-1])
                   for chain, seq in chains):
                accepted.update(run)
        return accepted

    def _run_matches(self, chain: list[str], seq: list[str], pids: list[str], ours: str,
                     base: str, last: str) -> bool:
        k = len(pids)
        for i in range(len(seq) - k + 1):
            if seq[i:i + k] == pids and self._tree_matches(ours, chain[i + k - 1], base, last):
                return True
        return False

    def _tree_matches(self, ours: str, theirs: str, base: str, last: str) -> bool:
        """True when merging `theirs` onto `ours` (merge base `base`) cleanly gives the tree of
        `last`. Any failure to compute the merge (conflict, unsupported git) is a mismatch."""
        proc = self._git(["merge-tree", "--write-tree", f"--merge-base={base}", ours, theirs],
                         check=False)
        if proc.returncode != 0 or not proc.stdout.strip():
            return False
        tree = proc.stdout.decode().split()[0]
        return tree == self._lines(["rev-parse", f"{last}^{{tree}}"])[0]
