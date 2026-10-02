"""Host-side file access in places a sandbox may write (a sandbox can plant links and files there).

Hooks, the CLI, bootstrap, the file board and the supervisor run outside any sandbox as the host
user, but read and write files in directories a sandboxed agent can also write: the state dir,
the spool, the marker dir, and (if a user widens the sandbox) the private dirs. Opening such a
path by name follows whatever the sandbox planted there: a symlink to ~/.bashrc or to a `.pth`
file of the swarm's venv, a hard link to either, a FIFO that blocks the reader forever, or a
huge file. Everything here is safe against that:

- Directories are reached through descriptors (`open_base`, `open_sub`): each component is
  opened with O_DIRECTORY | O_NOFOLLOW and checked (fstat) to be this user's and not writable
  by group or others unless sticky (or, above the first of this user's components outside the
  home directory, root's and not writable by others unless sticky, as /tmp is). A symlink at any component is refused (UnsafePathError).
- Files are opened relative to such a descriptor with O_NOFOLLOW | O_NONBLOCK and checked on the
  descriptor before any byte is read or written (`_check`): a regular file of this user with
  exactly one link, and, for reads, no larger than the limit. So a planted symlink is never
  followed, a planted hard link is never read or written, and a FIFO or device never blocks.
- Nothing is ever truncated in place. `write_atomic` writes a fresh O_EXCL file with a random
  name and renames it over the name, which replaces whatever entry is there without following it.
- Appends (`append`) use O_APPEND on the checked descriptor; `touch`, `lock` and `scan` likewise.
- `log_safe` makes any text a single printable log line.

UnsafePathError is a PermissionError (an OSError), so best-effort callers that already catch
OSError keep working. Names passed to the file functions are single directory entries: a name
with a slash, "", "." or ".." raises ValueError.

supervisor/privfs.py is this module applied to the supervisor's private directory."""
from __future__ import annotations

import contextlib
from swarm import compat
import os
import re
import secrets
import stat
from pathlib import Path

DIR_FLAGS = os.O_RDONLY | compat.O_DIRECTORY | compat.O_NOFOLLOW | compat.O_CLOEXEC
MAX_READ = 1 << 20          # read()'s default limit: 1 MiB


class UnsafePathError(PermissionError):
    """A path or file is not what a sandbox-safe open requires (a symlink, not a directory or
    regular file, another user's, a hard link, a loose mode, too big). The message says which."""


# --- directories -------------------------------------------------------------------------------

def _abs(path) -> Path:
    s = os.path.expanduser(os.fspath(path))
    if not os.path.isabs(s):
        raise ValueError(f"{s!r} is not an absolute path")
    if ".." in Path(s).parts:
        raise ValueError(f"{s!r} contains '..'")
    return Path(os.path.normpath(s))


def _entry(name: str) -> str:
    if not isinstance(name, str) or name in ("", ".", "..") or "/" in name or "\0" in name:
        raise ValueError(f"{name!r} is not a single directory entry name")
    return name


def _open_component(parent: int, name: str, where: str, create: bool, mode: int) -> tuple[int, bool]:
    """(descriptor, created) of directory `name` in `parent`, never through a symlink."""
    created = False
    if create:
        try:
            compat.mkdir(name, mode, dir_fd=parent)
            created = True
        except FileExistsError:
            pass
    try:
        fd = compat.open(name, DIR_FLAGS, dir_fd=parent)
    except FileNotFoundError:
        raise
    except OSError as exc:   # ELOOP (a symlink) or ENOTDIR
        raise UnsafePathError(f"{where} is a symlink or not a directory: refusing to follow it "
                              f"(it must be a real directory; remove it and run again)") from exc
    return fd, created


def _check_dir(fd: int, where: str, *, own: bool, strict_mode: int | None, created: bool) -> None:
    if not compat.HAS_MODES:   # Windows: no uid/mode model; the profile's NTFS ACLs are the privacy
        return
    st = os.fstat(fd)
    uid = compat.uid()
    if own:
        if st.st_uid != uid:
            raise UnsafePathError(f"{where} belongs to another user (uid {st.st_uid}): refusing to use it")
    elif st.st_uid not in (uid, 0):
        raise UnsafePathError(f"{where} belongs to another user (uid {st.st_uid}): refusing to use it")
    elif st.st_uid != uid and st.st_mode & stat.S_IWOTH and not st.st_mode & stat.S_ISVTX:
        raise UnsafePathError(f"{where} is writable by every user and not sticky: refusing to use a "
                              f"path through it")
    if strict_mode is not None:
        if created:
            compat.fchmod(fd, strict_mode)
        mode = stat.S_IMODE(os.fstat(fd).st_mode)
        if mode != strict_mode:
            raise UnsafePathError(f"{where} has mode {oct(mode)}, not {oct(strict_mode)}: refusing to "
                                  f"use it (check what is in it, then chmod {strict_mode:o} {where})")
    st = os.fstat(fd)
    if (st.st_uid == uid and st.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            and not st.st_mode & stat.S_ISVTX):
        raise UnsafePathError(f"{where} is writable by its group or every user (mode "
                              f"{oct(stat.S_IMODE(st.st_mode))}): refusing to use a path through it "
                              f"(chmod go-w {where})")


def open_base(path, *, create: bool = True, strict_mode: int | None = None) -> int:
    """A verified descriptor (O_DIRECTORY, close-on-exec) of the directory `path`; the caller
    closes it. `path` is absolute or starts with ~, and has no "..".

    Under the home directory: the walk starts at $HOME (itself opened normally) and every
    component below it is opened with O_NOFOLLOW and must be this user's. Elsewhere (/tmp/...):
    the walk starts at /, components must be root's (and not writable by others unless sticky)
    until the first one of this user's, and this user's from there on; the final directory
    must be this user's. None of this user's components may be group- or world-writable
    unless sticky (another user could swap its entries). Missing components are created 0700 when `create`. With `strict_mode`
    the final directory must have exactly that mode (a directory just created gets it).

    Raises UnsafePathError (a symlink or non-directory component, another user's, a loose
    mode), FileNotFoundError (missing and not `create`), ValueError (a bad path)."""
    target = _abs(path)
    home = Path(os.path.normpath(os.path.expanduser("~")))
    if target == home or home in target.parents:
        start, rel, inside_home = home, target.relative_to(home).parts, True
    else:
        start, rel, inside_home = Path(target.anchor), target.parts[1:], False
    fd = compat.open_root(start)
    where = start
    try:
        if not rel:
            _check_dir(fd, str(where), own=True, strict_mode=strict_mode, created=False)
            return fd
        mine = inside_home
        for i, part in enumerate(rel):
            last = i == len(rel) - 1
            where = where / part
            nfd, created = _open_component(fd, part, str(where), create, 0o700)
            os.close(fd)
            fd = nfd
            if not mine and os.fstat(fd).st_uid == compat.uid():
                mine = True
            _check_dir(fd, str(where), own=mine or last, strict_mode=strict_mode if last else None,
                       created=created)
        return fd
    except BaseException:
        os.close(fd)
        raise


def open_sub(parent: int, name: str, *, create: bool = True, strict_mode: int | None = None) -> int:
    """A verified descriptor of directory `name` inside the verified directory `parent`: opened
    with O_NOFOLLOW, this user's, created 0700 when missing and `create`, exactly `strict_mode`
    if given. Same errors as open_base."""
    _entry(name)
    fd, created = _open_component(parent, name, name, create, 0o700)
    try:
        _check_dir(fd, name, own=True, strict_mode=strict_mode, created=created)
    except BaseException:
        os.close(fd)
        raise
    return fd


@contextlib.contextmanager
def dir_fd(path, *, create: bool = True, strict_mode: int | None = None):
    """open_base as a context manager: yields the descriptor, closes it after."""
    fd = open_base(path, create=create, strict_mode=strict_mode)
    try:
        yield fd
    finally:
        os.close(fd)


# --- files -------------------------------------------------------------------------------------

def _check(fd: int, name: str, limit: int | None = None) -> None:
    """The open file is a regular file of this user with one link (and at most `limit` bytes)."""
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_uid != compat.uid() or st.st_nlink != 1:
        raise UnsafePathError(f"{name} is not a private regular file of this user (a planted link?): "
                              f"not using it")
    if limit is not None and st.st_size > limit:
        raise UnsafePathError(f"{name} is larger than {limit} bytes: not reading it")


def open_existing(d: int, name: str, flags: int) -> int:
    """`name` in directory `d`, opened without following a symlink or blocking (O_NOFOLLOW |
    O_NONBLOCK) and checked (_check); never created, never truncated."""
    flags &= ~(os.O_CREAT | os.O_TRUNC | os.O_EXCL)
    fd = compat.open(_entry(name), flags | compat.O_NOFOLLOW | compat.O_NONBLOCK | compat.O_CLOEXEC, dir_fd=d)
    try:
        _check(fd, name)
    except BaseException:
        os.close(fd)
        raise
    return fd


def create(d: int, name: str, mode: int = 0o600) -> int:
    """A new file `name` in `d` open for writing (O_CREAT | O_EXCL | O_NOFOLLOW): FileExistsError
    if anything is there."""
    return compat.open(_entry(name), os.O_WRONLY | os.O_CREAT | os.O_EXCL | compat.O_NOFOLLOW | compat.O_CLOEXEC,
                   mode, dir_fd=d)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


def read(d: int, name: str, limit: int = MAX_READ) -> bytes | None:
    """The content of `name` in `d`, or None if it is missing, isn't a regular file of this user
    with one link (a symlink, hard link, FIFO, directory...), or holds more than `limit` bytes.
    Never blocks, never follows a link."""
    _entry(name)
    try:
        fd = open_existing(d, name, os.O_RDONLY)
    except OSError:
        return None
    try:
        try:
            _check(fd, name, limit)
        except OSError:
            return None
        out = bytearray()
        while len(out) <= limit:
            chunk = os.read(fd, min(65536, limit + 1 - len(out)))
            if not chunk:
                break
            out += chunk
        return None if len(out) > limit else bytes(out)   # grew past the limit while read
    finally:
        os.close(fd)


def read_text(d: int, name: str, limit: int = MAX_READ) -> str | None:
    """read() decoded as UTF-8 (undecodable bytes replaced); None as for read()."""
    data = read(d, name, limit)
    return None if data is None else data.decode("utf-8", "replace")


def read_tail(d: int, name: str, limit: int) -> tuple[bytes, bool] | None:
    """The last `limit` bytes of `name` and whether earlier bytes were dropped; None if missing or
    refused."""
    try:
        fd = open_existing(d, name, os.O_RDONLY)
    except OSError:
        return None
    try:
        size = os.fstat(fd).st_size
        os.lseek(fd, max(0, size - limit), os.SEEK_SET)
        data = b""
        while len(data) < limit:   # never more than `limit` bytes, even if the file grows meanwhile
            chunk = os.read(fd, min(65536, limit - len(data)))
            if not chunk:
                break
            data += chunk
        return data, size > limit
    finally:
        os.close(fd)


def write_atomic(d: int, name: str, data: bytes | str, mode: int = 0o600) -> None:
    """Replace `name` in `d` by a new file holding `data`, mode `mode`: written to a fresh O_EXCL
    temp file with a random name, fsynced, then renamed over the name relative to `d`. A planted
    symlink, hard link or FIFO at `name` is replaced, never followed or truncated; a directory
    there makes it raise (OSError)."""
    _entry(name)
    if isinstance(data, str):
        data = data.encode("utf-8")
    tmp = f".{name[:200]}.{secrets.token_hex(6)}.swarm-tmp"
    fd = create(d, tmp, mode)
    try:
        compat.fchmod(fd, mode)
        _write_all(fd, data)
        os.fsync(fd)
        os.close(fd)
        fd = -1
        compat.rename(tmp, name, src_dir_fd=d, dst_dir_fd=d)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        with contextlib.suppress(OSError):
            compat.unlink(tmp, dir_fd=d)
        raise
    with contextlib.suppress(OSError):
        compat.fsync_dir(d)


def _open_or_create(d: int, name: str, flags: int, mode: int) -> int:
    """`name` in `d` opened with `flags` | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC, after the _check.
    Created if missing (O_EXCL) and then fchmod'ed to exactly `mode`, whatever the umask; an
    existing file looser than `mode` is tightened."""
    flags |= compat.O_NOFOLLOW | compat.O_NONBLOCK | compat.O_CLOEXEC
    _entry(name)
    for _ in range(3):   # an entry removed between the two opens: try again
        try:
            fd, created = compat.open(name, flags | os.O_CREAT | os.O_EXCL, mode, dir_fd=d), True
            break
        except FileExistsError:
            pass
        try:
            fd, created = compat.open(name, flags, dir_fd=d), False
            break
        except FileNotFoundError:
            continue
    else:
        raise FileNotFoundError(f"{name} keeps disappearing")
    try:
        _check(fd, name)
        if created:
            compat.fchmod(fd, mode)
        else:
            cur = stat.S_IMODE(os.fstat(fd).st_mode)
            if cur & ~mode:
                compat.fchmod(fd, cur & mode)
    except BaseException:
        os.close(fd)
        raise
    return fd


def append(d: int, name: str, text: str | bytes, mode: int = 0o600) -> None:
    """Append `text` to `name` in `d` in one O_APPEND write, after the _check on the descriptor.
    A new file gets exactly `mode` (default 0600, whatever the umask); an existing one looser
    than `mode` is tightened first. Raises OSError (UnsafePathError) for a planted link, FIFO,
    directory or another user's file, and writes nothing then."""
    fd = _open_or_create(d, name, os.O_WRONLY | os.O_APPEND, mode)
    try:
        _write_all(fd, text.encode("utf-8") if isinstance(text, str) else text)
    finally:
        os.close(fd)


def touch(d: int, name: str, mode: int = 0o600) -> None:
    """Create `name` in `d` empty with exactly `mode` (whatever the umask) if missing, else set its
    times to now through a checked descriptor (os.utime on the fd). Raises OSError for anything
    but a private regular file."""
    try:
        fd = create(d, name, mode)
    except FileExistsError:
        pass
    else:
        try:
            compat.fchmod(fd, mode)
        finally:
            os.close(fd)
        return
    fd = open_existing(d, name, os.O_RDONLY)
    try:
        compat.utime_fd(fd, name, d)
    finally:
        os.close(fd)


def open_lock(d: int, name: str) -> int:
    """`name` in `d` opened read-write for flock, created exactly 0600 if missing (never
    truncated), after the _check; not locked yet. Raises OSError for a planted link, FIFO or
    directory."""
    return _open_or_create(d, name, os.O_RDWR, 0o600)


def open_wlock(d: int, name: str) -> int:
    """A lock file that only a writer can hold: `name` in `d` opened write-only, created exactly
    0200 if missing, an existing looser one tightened to 0200 (after the _check: this user's
    regular file, one link, never through a link). flock needs no read access, and a process
    that may only read the file -- a sandbox with the directory in its read-only view -- can't
    open it at all, so it can't hold the lock. Not locked yet."""
    return _open_or_create(d, name, os.O_WRONLY, 0o200)


def lock(d: int, name: str, *, blocking: bool = True) -> int:
    """open_lock, then an exclusive flock on it: the descriptor (closing it releases the lock).
    blocking=False raises BlockingIOError when another holder has it."""
    fd = open_lock(d, name)
    try:
        compat.flock(fd, compat.LOCK_EX | (0 if blocking else compat.LOCK_NB))
    except BaseException:
        os.close(fd)
        raise
    return fd


@contextlib.contextmanager
def locked(d: int, name: str, *, blocking: bool = True):
    """lock() as a context manager: yields the locked descriptor, unlocks and closes it after."""
    fd = lock(d, name, blocking=blocking)
    try:
        yield fd
    finally:
        with contextlib.suppress(OSError):
            compat.flock(fd, compat.LOCK_UN)
        os.close(fd)


def scan(d: int, suffix: str = "", *, limit: int = MAX_READ, match=None,
         skipped: list | None = None) -> list[tuple[str, bytes]]:
    """[(name, content)] of the entries of `d` whose name ends with `suffix` (and passes
    `match(name)` if given), sorted by name, each read through read(): anything that isn't a
    private regular file with one link of at most `limit` bytes is left out, and its name added
    to `skipped` if given. Never blocks, never follows a link."""
    out = []
    for name in sorted(compat.listdir(d)):
        if not name.endswith(suffix) or (match is not None and not match(name)):
            continue
        data = read(d, name, limit)
        if data is None:
            if skipped is not None:
                skipped.append(name)
            continue
        out.append((name, data))
    return out


def fresh_file(d: int, name: str) -> tuple[int, str | None]:
    """A new, empty `name` in `d` open for writing. Anything already at `name` (a leftover, or
    something planted) is moved aside to a random name first, never opened.
    (descriptor, the name it was moved to or None)."""
    try:
        return create(d, name), None
    except FileExistsError:
        moved = f"{name}.stale-{secrets.token_hex(6)}"
        compat.rename(name, moved, src_dir_fd=d, dst_dir_fd=d)
    return create(d, name), moved


def unlink(d: int, name: str) -> None:
    """Remove the entry `name` of `d` (a link is removed, not followed); missing is fine."""
    with contextlib.suppress(FileNotFoundError):
        compat.unlink(_entry(name), dir_fd=d)


def exists(d: int, name: str) -> bool:
    """Whether `d` has an entry `name` of any kind (a dangling symlink counts)."""
    try:
        compat.stat(_entry(name), dir_fd=d, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False


def mtime(d: int, name: str) -> float | None:
    """The entry's own modification time (lstat), or None."""
    try:
        return compat.stat(_entry(name), dir_fd=d, follow_symlinks=False).st_mtime
    except OSError:
        return None


# --- log lines ---------------------------------------------------------------------------------

_LOG_UNSAFE = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029\ud800-\udfff]")


def _log_escape(m: re.Match) -> str:
    o = ord(m.group())
    return f"\\x{o:02x}" if o < 0x100 else f"\\u{o:04x}"


def log_safe(s) -> str:
    """`s` (any value, via str) as one printable log line: C0 controls (newline, carriage return,
    tab, NUL, ESC...), DEL and C1 become \\xNN; U+2028/U+2029 and lone surrogates become
    \\uNNNN. Every log line built from board, marker or payload data goes through this."""
    return _LOG_UNSAFE.sub(_log_escape, str(s))
