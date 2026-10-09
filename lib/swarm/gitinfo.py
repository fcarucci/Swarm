"""The repository and HEAD commit of a work dir, read from its .git files without running git.

Used by the PreToolUse hook to fill in the `swarm ci wait --repo R --sha S` it suggests. The hook
runs outside the agent's sandbox and the work dir is the agent's, so nothing here executes git (a
repo's config can name programs to run) or follows a symlink, and nothing blocks on a FIFO or
reads a big file: every read is O_NOFOLLOW|O_NONBLOCK, of a regular file, at most MAX_BYTES.
Anything unusual gives None.
"""
from __future__ import annotations

import os
import re
import stat

from swarm import compat

MAX_BYTES = 256 * 1024
_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def _read(path: str) -> str | None:
    try:
        fd = compat.open(path, os.O_RDONLY | compat.O_NOFOLLOW | compat.O_NONBLOCK | compat.O_CLOEXEC)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        data = os.read(fd, MAX_BYTES + 1)
        return None if len(data) > MAX_BYTES else data.decode("utf-8", "replace")
    except OSError:
        return None
    finally:
        os.close(fd)


def git_dirs(cwd: str, max_up: int = 40) -> tuple[str, str] | None:
    """(git dir, common dir) of the repository containing `cwd` (a worktree's .git file points at
    its own git dir, whose `commondir` names the shared one), else None."""
    d = os.path.abspath(cwd)
    for _ in range(max_up):
        dot = os.path.join(d, ".git")
        try:
            st = os.lstat(dot)
        except OSError:
            st = None
        if st is not None and stat.S_ISDIR(st.st_mode):
            return dot, dot
        if st is not None and stat.S_ISREG(st.st_mode):
            text = _read(dot) or ""
            m = re.match(r"gitdir: (.+)", text.strip())
            if not m:
                return None
            gd = m.group(1).strip()
            gd = gd if os.path.isabs(gd) else os.path.normpath(os.path.join(d, gd))
            common = (_read(os.path.join(gd, "commondir")) or "").strip()
            common = (common if os.path.isabs(common) else os.path.normpath(os.path.join(gd, common))) if common else gd
            return gd, common
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent
    return None


def head_sha(cwd: str) -> str | None:
    dirs = git_dirs(cwd)
    if dirs is None:
        return None
    gd, common = dirs
    head = (_read(os.path.join(gd, "HEAD")) or "").strip()
    if _SHA.fullmatch(head):
        return head
    m = re.fullmatch(r"ref: (refs/[A-Za-z0-9._/-]+)", head)
    if not m or ".." in m.group(1):
        return None
    ref = m.group(1)
    for base in (gd, common):
        value = (_read(os.path.join(base, ref)) or "").strip()
        if _SHA.fullmatch(value):
            return value
    for line in (_read(os.path.join(common, "packed-refs")) or "").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == ref and _SHA.fullmatch(parts[0]):
            return parts[0]
    return None


def _owner_repo(url: str) -> str | None:
    m = re.search(r"[:/]([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$", url.strip())
    return f"{m.group(1)}/{m.group(2)}" if m else None


def remotes(cwd: str) -> dict[str, str]:
    """{remote name: url} from the repository's config."""
    dirs = git_dirs(cwd)
    if dirs is None:
        return {}
    out, name = {}, None
    for line in (_read(os.path.join(dirs[1], "config")) or "").splitlines():
        line = line.strip()
        m = re.fullmatch(r'\[remote "([^"]+)"\]', line)
        if m:
            name = m.group(1)
            continue
        if line.startswith("["):
            name = None
            continue
        m = re.fullmatch(r"url\s*=\s*(\S+)", line)
        if name and m and name not in out:
            out[name] = m.group(1)
    return out


def repo_slug(cwd: str, prefer_github: bool = True) -> str | None:
    """OWNER/REPO of the work dir's remote: a github.com one first (prefer_github), else
    origin, else the first."""
    rs = remotes(cwd)
    order = sorted(rs, key=lambda n: (not (prefer_github and "github.com" in rs[n]), n != "origin"))
    for name in order:
        slug = _owner_repo(rs[name])
        if slug:
            return slug
    return None
