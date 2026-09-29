"""File access in the supervisor's private directory (~/.local/share/swarm/supervisor), safe even
if a sandboxed agent can write it (a user-added Codex writable
root, or a Codex session rooted at $HOME, covers it).

- Directories are reached through descriptors: from the home directory, each component below it
  is opened with O_NOFOLLOW | O_DIRECTORY (created 0700 if missing) and must be this user's; the
  private directory and its runs directory must also be mode 0700. A symlink anywhere below home
  is refused (PrivateDirError).
- Files are opened relative to that descriptor with O_NOFOLLOW, and checked on the descriptor
  (fstat): a regular file of this user with one link. So a planted symlink is never followed and
  a planted hard link to a file elsewhere is never read or written.
- Files are created with O_CREAT | O_EXCL, never truncated. A rewrite is a new file (O_EXCL, random
  name) renamed over the old name, which replaces whatever entry is there without following it.
  A session's output files (fresh_file) that already exist are moved aside to a random name.
- Appends (the log) open with O_APPEND | O_NOFOLLOW and pass the same fstat check.

Nothing here follows a path the sandbox could have planted, so the guarantee doesn't depend on
the directory being unwritable (settings.codex_exposure detects when it is, and the pass
refuses to run).

The implementation is swarm.safefs (generalised to every
sandbox-writable place); this module fixes its base to the private directory, strict 0700, and
turns safefs.UnsafePathError on the directories into PrivateDirError."""
from __future__ import annotations

import contextlib
import os

from swarm import safefs
from swarm.safefs import (DIR_FLAGS, MAX_READ, _check, append, create, exists, fresh_file,  # noqa: F401
                          mtime, open_existing, read, read_tail, unlink, write_atomic)

RUNS = "replacements"
MODE = 0o700

# privfs.lock opens the lock file (checked, never truncated) without taking the flock: the
# runner and the pass take it themselves (blocking or not).
lock = safefs.open_lock


def _error(msg: str):
    from swarm.supervisor.settings import PrivateDirError
    return PrivateDirError(msg)


def open_dir(sub: str | None = None, create: bool = True) -> int:
    """A verified descriptor of the private directory (or its `sub` directory): every component
    from the home directory down is a real directory of this user, and the private directory
    and `sub` are mode 0700 (safefs.open_base / open_sub). The caller closes it.
    PrivateDirError when that fails; FileNotFoundError when it doesn't exist and create is False."""
    from swarm.supervisor.settings import private_dir
    base = private_dir()
    try:
        fd = safefs.open_base(base, create=create, strict_mode=MODE)
        if sub is None:
            return fd
        try:
            return safefs.open_sub(fd, sub, create=create, strict_mode=MODE)
        finally:
            os.close(fd)
    except safefs.UnsafePathError as exc:
        where = f"{base / sub}" if sub else f"{base}"
        raise _error(f"the supervisor's private directory {where} is unsafe: {exc}") from exc


@contextlib.contextmanager
def dir_fd(sub: str | None = None, create: bool = True):
    fd = open_dir(sub, create)
    try:
        yield fd
    finally:
        os.close(fd)
