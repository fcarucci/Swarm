"""Pinned-remote git hygiene: the one place that says how a git call that takes a pinned URL runs
(drive activation's `remote_head`, Task 6a; the supervisor's mirrors and landing, Task 11b).

A pinned URL comes from a work dir's .git/config or a flag, and stays in the host-private owner
record, so it is checked wherever it enters, and every git call that takes it runs so that nothing a
seat can write decides what git does:

- check_url: no option-like URL (a leading "-": `--upload-pack=CMD` would run CMD), no transport
  helper (`ext::`, `fd::`), no whitespace or control characters, and only the pinned transports:
  https://, ssh:// or scp-style [user@]host:path, file:// or an absolute local path;
- url_args: `--` before the URL, always;
- git_env: no system or global config (GIT_CONFIG_NOSYSTEM=1, GIT_CONFIG_GLOBAL=/dev/null), the
  transports allowed by name (GIT_ALLOW_PROTOCOL overrides any protocol.* config), no prompts;
- SAFE_CONFIG (as `-c` pairs, git_argv): the same protocol allowlist, hooks and fsmonitor off;
- NEUTRAL_CWD: git runs outside any work dir, so it reads no repository config there;
- local transports (file:// or an absolute path): the path is percent-decoded and refused when any
  segment is ".." (git decodes %2e%2e); resolve_local gives its realpath and locality says whether a URL
  is local and lies under a root, whatever credential is pinned; local_pack_args runs the destination's
  upload-pack/receive-pack with its config defused (hooks, fsmonitor, alternateRefsCommand off and
  receive.denyCurrentBranch=refuse), because a local transport runs that repository's own git;
- check_pin: a stored credential pin is re-checked against the helper allowlist when it is read back;
- dir_problem: a config dir a helper reads (GH_CONFIG_DIR) must be this user's, not group- or
  world-writable, in a parent chain only root or this user can change, and not under a work root.

Nothing here imports swarm modules, so the enrolment validator, drive activation and the supervisor
can all use it."""
from __future__ import annotations

import os
import re
import stat
import urllib.parse

ALLOWED_PROTOCOLS = ("https", "ssh", "file")
ALLOW_PROTOCOL = ":".join(ALLOWED_PROTOCOLS)
SAFE_CONFIG = ("core.hooksPath=" + os.devnull, "core.fsmonitor=false", "protocol.allow=never",
               *(f"protocol.{p}.allow=always" for p in ALLOWED_PROTOCOLS))
NEUTRAL_CWD = os.path.abspath(os.sep)
# The helper commands a pinned credential may name: the absolute executable's basename and the fixed
# arguments it takes ([G2]). Anything else is refused at activation and when a pin is read back.
CREDENTIAL_HELPERS = {"gh": ("auth", "git-credential"), "git-credential-manager": (),
                      "git-credential-store": (), "git-credential-cache": ()}
URL_MAX = 4096
_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")
_HELPER_TRANSPORT = re.compile(r"[A-Za-z0-9][A-Za-z0-9+.-]*::")       # <transport>::<address>
SCP_URL = re.compile(r"(?:(?P<user>[^@/\\\s:]+)@)?(?P<host>[^@/\\\s:]{2,}):(?!//)")   # [user@]host:path


class GitPinError(ValueError):
    """A URL, pin, branch or directory that a pinned git call refuses (the message says why)."""


def no_controls(value: str) -> bool:
    return not any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value)


def _decoded(text: str) -> str:
    for _ in range(3):
        decoded = urllib.parse.unquote(text)
        if decoded == text:
            break
        text = decoded
    return text


def _optionlike(part) -> bool:
    """Whether a URL part starts with "-" once percent-decoded (%2d, %2D, and %252d decoded again)."""
    return _decoded(part or "").startswith("-")


def _local_path(url: str) -> str | None:
    """The filesystem path of a local URL as git reads it (a file:// path percent-decoded), or None for a
    network URL. A local URL with any '%' is refused by _check_local, so both readings agree."""
    if url.lower().startswith("file://"):
        return _decoded(urllib.parse.urlsplit(url).path)
    if "://" in url or (SCP_URL.match(url) and not os.path.isabs(url)):
        return None
    return _decoded(url)


def _check_local(url: str) -> None:
    path = _local_path(url)
    if path is None:
        return
    raw = urllib.parse.urlsplit(url).path if url.lower().startswith("file://") else url
    if "%" in raw:
        # git decodes a file:// path (%2e%2e is ".."), and opens a plain path literally: a local remote
        # with any '%' would mean two different directories, so it is refused outright
        raise GitPinError("a local remote must not contain '%' (git percent-decodes file:// paths)")
    if not no_controls(path):
        raise GitPinError("a local remote must not contain (encoded) whitespace or control characters")
    if ".." in path.replace(os.sep, "/").split("/"):
        raise GitPinError("a local remote must not contain a '..' segment (also percent-encoded)")


def check_url(url) -> str:
    """The URL unchanged when a pinned git call may take it, else GitPinError."""
    if not isinstance(url, str) or not url or len(url) > URL_MAX:
        raise GitPinError("a remote URL must be a non-empty string")
    if not no_controls(url):
        raise GitPinError("a remote URL must not contain whitespace or control characters")
    if url.startswith("-"):
        raise GitPinError("a remote URL must not start with '-' (git would read it as an option)")
    if _HELPER_TRANSPORT.match(url):
        raise GitPinError(f"a remote URL must use {', '.join(ALLOWED_PROTOCOLS)}, not a transport helper")
    if "://" in url:
        try:
            parts = urllib.parse.urlsplit(url)
            host = parts.hostname
        except ValueError:
            raise GitPinError("a remote URL is not a valid URL") from None
        scheme = parts.scheme.lower()
        if scheme not in ALLOWED_PROTOCOLS:
            raise GitPinError(f"a remote URL must use {', '.join(ALLOWED_PROTOCOLS)}, not {scheme or 'no'} transport")
        if parts.password is not None or (parts.username is not None and scheme != "ssh"):
            raise GitPinError("a remote URL must not carry credentials")
        if "\\" in url:
            raise GitPinError("a remote URL must not contain a backslash")
        if any(_optionlike(seg) for seg in parts.path.split("/")):
            raise GitPinError("a remote URL path must not start a segment with '-' (also percent-encoded)")
        if scheme == "file":
            if parts.netloc or not os.path.isabs(parts.path):
                raise GitPinError("a file:// remote must name an absolute local path")
            _check_local(url)
            return url
        if not host or _optionlike(host) or _optionlike(parts.username):
            raise GitPinError("a remote URL must name a host that is not an option")
        try:
            port = parts.port
        except ValueError:
            port = -1
        if port is not None and not 1 <= port <= 65535:
            raise GitPinError("a remote URL port must be 1-65535")
        return url
    m = SCP_URL.match(url)
    if m and not os.path.isabs(url):
        if _optionlike(m.group("host")) or _optionlike(m.group("user")):
            raise GitPinError("a remote URL must name a host that is not an option")
        if any(_optionlike(seg) for seg in url[m.end():].split("/")):
            raise GitPinError("a remote URL path must not start a segment with '-' (also percent-encoded)")
        return url
    if not os.path.isabs(url):
        raise GitPinError("a local remote must be an absolute path")
    _check_local(url)
    return url


def is_local(url: str) -> bool:
    """Whether git reaches `url` through the local transport (file:// or an absolute path), which runs
    the destination repository's own upload-pack/receive-pack; checked first (GitPinError when refused)."""
    return _local_path(check_url(url)) is not None


def resolve_local(url: str) -> str:
    """The realpath of a local URL's repository, as git will open it (decoded, symlinks resolved).
    GitPinError for a refused or a network URL."""
    path = _local_path(check_url(url))
    if path is None:
        raise GitPinError("not a local remote")
    return os.path.realpath(path)


def locality(url: str, root: str | None = None) -> tuple:
    """(local, under): whether `url` is local, and whether its realpath lies under realpath(`root`) (False
    for a network URL or no root). Decided from the URL alone, for every credential kind: a local
    transport ignores an ssh or helper pin."""
    if not is_local(url):
        return False, False
    if root is None:
        return True, False
    return True, _under(resolve_local(url), os.path.realpath(root))


# The destination's own config, when git runs its upload-pack/receive-pack locally: no hooks, no
# fsmonitor, no alternateRefsCommand (it runs a shell command from that repository's config), and no
# push into a checked-out branch (updateInstead would run fsmonitor and smudge filters in its worktree).
LOCAL_PACK_CONFIG = ("core.hooksPath=" + os.devnull, "core.fsmonitor=false", "core.alternateRefsCommand=",
                     "receive.denyCurrentBranch=refuse", "receive.denyDeleteCurrent=refuse")


def local_pack_args(op: str, git: str = "git") -> list:
    """The pack-program flag for a local destination, for `op` "push" (["--receive-pack=<cmd>"]) or a
    read: "fetch", "ls-remote" or "clone" (["--upload-pack=<cmd>"]). <cmd> is
    `git -c <LOCAL_PACK_CONFIG>... receive-pack|upload-pack`, shell-quoted (git runs it with sh).
    Callers add it only for a local URL (is_local)."""
    import shlex
    if op == "push":
        flag, which = "--receive-pack", "receive-pack"
    elif op in ("fetch", "ls-remote", "clone"):
        flag, which = "--upload-pack", "upload-pack"
    else:
        raise GitPinError(f"local_pack_args: unknown git operation {op!r}")
    parts = [git]
    for item in LOCAL_PACK_CONFIG:
        parts += ["-c", item]
    return [f"{flag}=" + " ".join(shlex.quote(p) for p in parts + [which])]


def check_branch(branch) -> str:
    """A branch name a pinned call may use as refs/heads/<branch>."""
    if (not isinstance(branch, str) or not _BRANCH.fullmatch(branch) or ".." in branch
            or branch.endswith(("/", ".", ".lock")) or "//" in branch):
        raise GitPinError(f"{branch!r:.80} is not a branch name")
    return branch


def check_pin(pin) -> str:
    """A credential pin as the owner record stores it: `helper:<absolute path>[ args]` on the
    allowlist, `ssh-key:<absolute path>` or `ssh-agent:<absolute socket path>`."""
    if not isinstance(pin, str) or not pin or len(pin) > URL_MAX or any(ord(c) < 32 or ord(c) == 127 for c in pin):
        raise GitPinError("a credential pin must be a non-empty string without control characters")
    kind, sep, value = pin.partition(":")
    if not sep:
        raise GitPinError("a credential pin is helper:<path>, ssh-key:<path> or ssh-agent:<socket>")
    if kind == "helper":
        exe, *args = value.split(" ")
        if not os.path.isabs(exe):
            raise GitPinError("a pinned credential helper must be an absolute path")
        base = os.path.basename(exe)
        if base not in CREDENTIAL_HELPERS or tuple(args) != CREDENTIAL_HELPERS[base]:
            raise GitPinError(f"credential helper {base!r} is not on the allowlist "
                              f"({', '.join(CREDENTIAL_HELPERS)}) with its fixed arguments")
        return pin
    if kind in ("ssh-key", "ssh-agent"):
        if not os.path.isabs(value) or any(c.isspace() for c in value):
            raise GitPinError(f"a pinned {kind} must be an absolute path without spaces")
        return pin
    raise GitPinError("a credential pin is helper:<path>, ssh-key:<path> or ssh-agent:<socket>")


def git_env(*, home: str, path: str | None = None, extra: dict | None = None) -> dict:
    """The whole environment of a pinned git call: nothing ambient except PATH (the person's, or `path`)."""
    env = {"PATH": os.environ.get("PATH", "") if path is None else path, "HOME": home, "LC_ALL": "C",
           "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
           "GIT_ALLOW_PROTOCOL": ALLOW_PROTOCOL}
    env.update(extra or {})
    return env


def git_argv(git: str, *args: str) -> list:
    """[git, the SAFE_CONFIG `-c` pairs, *args]."""
    out = [git]
    for item in SAFE_CONFIG:
        out += ["-c", item]
    return out + list(args)


def url_args(url: str, *rest: str) -> list:
    """["--", url, *rest]: the checked URL, never readable as an option."""
    return ["--", check_url(url), *rest]


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def dir_problem(path: str, forbidden_roots=()) -> str | None:
    """Why `path` cannot be trusted as a helper's private config dir (None when it can): missing or
    not a directory; under a root a seat can write (`forbidden_roots`); not owned by this user, or
    writable by group or others; or a parent directory another user, or group or others, can change
    (a sticky directory such as /tmp is allowed as a parent: others cannot replace our entry)."""
    if not isinstance(path, str) or not os.path.isabs(path):
        return "is not an absolute path"
    real = os.path.realpath(path)
    for root in forbidden_roots:
        if _under(real, os.path.realpath(root)):
            return f"lies under the work root {root} (a seat could write it)"
    try:
        st = os.stat(real)
    except OSError:
        return "does not exist"
    if not stat.S_ISDIR(st.st_mode):
        return "is not a directory"
    if os.name == "nt":
        return None
    uid = os.getuid()
    if st.st_uid != uid:
        return "is not owned by this user"
    if st.st_mode & 0o022:
        return "is writable by group or others"
    node = os.path.dirname(real)
    while True:
        try:
            pst = os.stat(node)
        except OSError:
            return f"has an unreadable parent {node}"
        if pst.st_uid not in (0, uid) or (pst.st_mode & 0o022 and not pst.st_mode & stat.S_ISVTX):
            return f"has a parent {node} that another user, or group or others, can change"
        parent = os.path.dirname(node)
        if parent == node:
            return None
        node = parent
