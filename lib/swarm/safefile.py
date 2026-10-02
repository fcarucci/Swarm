"""Rewriting a user's file safely (settings.json, ~/.codex/config.toml): a temp file in the same
directory with the original's permissions capped at 0600 (never looser, tighter kept), owner
kept when allowed, fsync, atomic rename, directory fsync. Backups are always 0600.

Two variants for files the swarm creates itself: create_exclusive (a new file that appears
complete, never over an existing one: the supervisor's resume markers) and append_private (an
append-only log: the supervisor's log)."""
from __future__ import annotations

import os
import stat
import tempfile
import time
from pathlib import Path
from swarm import compat


CAP = 0o600   # user files the swarm rewrites may hold secrets: never looser than this


def write_preserving(path: Path, text: str, mode: int | None = None) -> None:
    st = path.stat() if path.exists() else None
    if mode is None:
        mode = stat.S_IMODE(st.st_mode) if st else CAP
    mode &= CAP                     # never looser than 0600, whatever the original or the caller says
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".swarm-tmp")   # created 0600
    try:
        compat.fchmod(fd, mode)
        if st is not None:
            try:
                compat.fchown(fd, st.st_uid, st.st_gid)
            except PermissionError:
                pass
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _fsync_dir(path.parent)


def _fsync_dir(d: Path) -> None:
    try:
        if compat.IS_WINDOWS:
            return
        dfd = compat.open(d, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


def _temp_with(path: Path, text: str, mode: int) -> str:
    """A fsynced temp file beside `path` (name .<name>.*.swarm-tmp) holding `text`, mode capped."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".swarm-tmp")   # created 0600
    try:
        compat.fchmod(fd, mode & CAP)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        os.unlink(tmp)
        raise
    return tmp


def create_exclusive(path: Path, text: str, mode: int = CAP) -> None:
    """Create `path` holding `text` (mode capped at 0600): written to a temp file first, then
    hard-linked into place, so it appears complete or not at all. Raises FileExistsError, and
    changes nothing, if anything is at `path` already (a link never replaces)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _temp_with(path, text, mode)
    try:
        os.link(tmp, path)
    finally:
        os.unlink(tmp)
    _fsync_dir(path.parent)


def append_private(path: Path, text: str) -> None:
    """Append `text` to the log at `path`, created 0600. write_preserving doesn't fit a log: it
    rewrites the whole file on every line, and two writers at once (supervisor, runners) would
    lose each other's lines to the last rename. O_APPEND writes of one short line don't
    interleave. Refuses (OSError) a symlink (O_NOFOLLOW), anything but a regular file, a file
    of another user, or a hard link (more than one link: it passes O_NOFOLLOW); a looser mode
    is tightened to 0600 first."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = compat.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | compat.O_NOFOLLOW | compat.O_NONBLOCK
                 | compat.O_CLOEXEC, CAP)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != compat.uid() or st.st_nlink != 1:
            raise PermissionError(f"{path} is not a regular file of this user with one link (a "
                                  f"planted hard link?); not writing to it")
        if stat.S_IMODE(st.st_mode) & ~CAP:
            compat.fchmod(fd, stat.S_IMODE(st.st_mode) & CAP)
        data = text.encode("utf-8")
        while data:
            data = data[os.write(fd, data):]
    finally:
        os.close(fd)


def backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    dst = path.with_name(f"{path.name}.pre-swarm-{time.strftime('%Y%m%d-%H%M%S')}")
    write_preserving(dst, path.read_text(encoding="utf-8"), mode=CAP)
    return dst
