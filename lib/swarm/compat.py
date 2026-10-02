"""Every platform switch of the swarm lives here; the rest of lib/swarm calls this module and never
tests sys.platform itself.

On POSIX each name below is the plain os / fcntl function (an alias, not a wrapper), so behaviour
there is unchanged. On Windows:

- `flock` is LockFileEx on a byte far beyond any data (shared or exclusive, blocking or not);
  a failed non-blocking try raises BlockingIOError like fcntl.flock.
- Directory "descriptors" (safefs.open_base, open_sub) cannot be real descriptors: a directory
  descriptor is an ordinary descriptor of the null device registered in a table that maps it to
  the directory's path, so os.close() still works on it. The fd-relative functions (`open`,
  `stat`, `lstat`, `rename`, `unlink`, `mkdir`, `link`, `utime`, `listdir`, `chmod`) resolve a
  `dir_fd` through that table, and refuse a symlink or reparse point (junction) where POSIX
  uses O_NOFOLLOW.
- Unix ownership and mode checks have no equivalent (files in the user's profile are private by
  their NTFS ACLs): `uid()` is 0, which is what os.stat reports as st_uid there, and chmod/fchmod
  are no-ops. The 0600/0700 mode checks are skipped by their callers through `HAS_MODES`.
- Features that need Linux (systemd scopes, process groups, pidfd) call `require_posix`, which
  raises Unsupported with a clear message.
"""
from __future__ import annotations

import errno
import os
import sys

IS_WINDOWS = sys.platform == "win32"
HAS_MODES = not IS_WINDOWS


class Unsupported(RuntimeError):
    """A feature that this platform cannot provide (the message says which and what to do)."""


def require_posix(feature: str) -> None:
    """Raise Unsupported on Windows: `feature` needs a POSIX system (Linux with systemd, macOS)."""
    if IS_WINDOWS:
        raise Unsupported(f"{feature} is not supported on Windows (it needs POSIX process groups and "
                          f"systemd); run it on Linux or WSL")


def setup_stdio() -> None:
    """Windows: the console's legacy code page can't carry agent names or board text; use UTF-8
    (hook payloads on stdin are UTF-8 JSON). POSIX: unchanged."""
    if IS_WINDOWS:
        for s in (sys.stdin, sys.stdout, sys.stderr):
            try:
                s.reconfigure(encoding="utf-8", errors="replace" if s is not sys.stdin else "strict")
            except (AttributeError, ValueError):
                pass


def node() -> str:
    """The host name (os.uname().nodename on POSIX)."""
    if IS_WINDOWS:
        import platform
        return platform.node()
    return os.uname().nodename


if not IS_WINDOWS:
    import fcntl

    LOCK_SH, LOCK_EX, LOCK_NB, LOCK_UN = fcntl.LOCK_SH, fcntl.LOCK_EX, fcntl.LOCK_NB, fcntl.LOCK_UN
    O_DIRECTORY, O_NOFOLLOW, O_NONBLOCK, O_CLOEXEC, O_BINARY = (
        os.O_DIRECTORY, os.O_NOFOLLOW, os.O_NONBLOCK, os.O_CLOEXEC, 0)

    def _late(name):
        # looked up on os at call time (not an alias bound at import), so a test patching os.<name>
        # still reaches the code that goes through here
        def call(*args, **kwargs):
            return getattr(os, name)(*args, **kwargs)
        call.__name__ = name
        return call

    uid = _late("getuid")
    fchmod, fchown, chmod = _late("fchmod"), _late("fchown"), _late("chmod")
    open = _late("open")      # noqa: A001  (the fd-relative family, same signatures as os.*)
    stat, lstat, rename, unlink = _late("stat"), _late("lstat"), _late("rename"), _late("unlink")
    mkdir, link, utime, listdir = _late("mkdir"), _late("link"), _late("utime"), _late("listdir")

    def claim_rename(src, dst, *, dir_fd) -> None:
        """Claim the record `src` by renaming it to `dst` (a name that differs per process): of
        several racing claimers exactly one succeeds, the rest get FileNotFoundError."""
        os.rename(src, dst, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)

    def flock(fd, op):
        return fcntl.flock(fd, op)

    def utime_fd(fd: int, name=None, dir_fd=None) -> None:
        """Set the times of the open file `fd` to now."""
        os.utime(fd)

    def replace(src, dst) -> None:
        os.replace(src, dst)

    def open_root(path) -> int:
        """A descriptor of directory `path` as given (a symlink at `path` itself is followed)."""
        return os.open(path, os.O_RDONLY | O_DIRECTORY | O_CLOEXEC)

    def fsync_dir(fd: int) -> None:
        os.fsync(fd)

    def open_dir(path) -> int:
        """A descriptor of directory `path`, opened without following a symlink."""
        return os.open(path, os.O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)

    def popen_detach() -> dict:
        """Popen keyword arguments that start the child in its own session."""
        return {"start_new_session": True}

else:
    import msvcrt
    import stat as _stat
    import time

    LOCK_SH, LOCK_EX, LOCK_NB, LOCK_UN = 1, 2, 4, 8
    O_DIRECTORY = 1 << 30     # private marker bit, stripped by open(); not an os.open flag
    O_NOFOLLOW = O_NONBLOCK = O_CLOEXEC = 0
    O_BINARY = os.O_BINARY
    _dirs: dict[int, str] = {}

    def uid() -> int:
        return 0

    def fchmod(fd, mode) -> None:
        return None

    def fchown(fd, uid_, gid) -> None:
        return None

    def chmod(path, mode, *, dir_fd=None, follow_symlinks=True) -> None:
        return None

    def _path(name, dir_fd) -> str:
        if dir_fd is None:
            return os.fspath(name)
        return os.path.join(_dirs[dir_fd], os.fspath(name))

    def _refuse_link(path: str) -> None:
        """OSError ELOOP if `path` is a symlink or a reparse point (junction); missing is fine."""
        try:
            st = os.lstat(path)
        except OSError:
            return
        if _stat.S_ISLNK(st.st_mode) or getattr(st, "st_file_attributes", 0) & _stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise OSError(errno.ELOOP, "refusing to follow a symbolic link or junction", path)

    def _need_dir(p: str) -> None:
        if not os.path.isdir(p):
            if not os.path.lexists(p):
                raise FileNotFoundError(errno.ENOENT, "no such directory", p)
            raise NotADirectoryError(errno.ENOTDIR, "not a directory", p)

    def open_dir(path) -> int:
        p = os.fspath(path)
        _refuse_link(p)
        _need_dir(p)
        fd = os.open(os.devnull, os.O_RDONLY | os.O_BINARY)
        _dirs[fd] = p
        return fd

    def open(path, flags, mode=0o777, *, dir_fd=None) -> int:   # noqa: A001
        p = _path(path, dir_fd)
        if flags & O_DIRECTORY:
            return open_dir(p)
        flags &= ~(O_DIRECTORY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC)
        if flags & os.O_EXCL and flags & os.O_CREAT:
            if os.path.lexists(p):   # a link counts too (POSIX: EEXIST); CREATE_NEW would follow a dangling one
                raise FileExistsError(errno.EEXIST, "file exists", p)
        else:
            _refuse_link(p)
        return _create_file(p, flags)

    def _create_file(p: str, flags: int) -> int:
        """os.open through CreateFileW with FILE_SHARE_DELETE: the C runtime's open() refuses to let
        anyone rename or delete a file while it is open, but the swarm replaces files (atomic
        writes, markers) that another descriptor of the same process still has open, as POSIX
        allows."""
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        acc = flags & (os.O_RDONLY | os.O_WRONLY | os.O_RDWR)
        access = {os.O_RDONLY: 0x80000000, os.O_WRONLY: 0x40000000, os.O_RDWR: 0xC0000000}[acc]
        if flags & os.O_APPEND:
            access = (access & ~0x40000000) | 0x4   # FILE_APPEND_DATA instead of GENERIC_WRITE
        creat, excl, trunc = bool(flags & os.O_CREAT), bool(flags & os.O_EXCL), bool(flags & os.O_TRUNC)
        disp = (1 if excl else 2 if trunc else 4) if creat else (5 if trunc else 3)
        h = k32.CreateFileW(p, access, 7, None, disp, 0x80 | 0x00200000, None)   # share rwd; NORMAL | OPEN_REPARSE_POINT
        if h in (None, wintypes.HANDLE(-1).value):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return msvcrt.open_osfhandle(h, (flags & (os.O_APPEND | os.O_RDONLY)) | O_BINARY | os.O_NOINHERIT)
        except BaseException:
            k32.CloseHandle(h)
            raise

    def stat(path, *, dir_fd=None, follow_symlinks=True):
        return os.stat(_path(path, dir_fd), follow_symlinks=follow_symlinks)

    def lstat(path, *, dir_fd=None):
        return os.lstat(_path(path, dir_fd))

    def _retry(fn, *args):
        """Windows refuses to replace or delete a file another process has open: wait briefly."""
        for i in range(20):
            try:
                return fn(*args)
            except PermissionError:
                if i == 19:
                    raise
                time.sleep(0.05)

    def rename(src, dst, *, src_dir_fd=None, dst_dir_fd=None) -> None:
        # os.rename refuses to overwrite on Windows; POSIX rename replaces atomically
        s, d = _path(src, src_dir_fd), _path(dst, dst_dir_fd)
        for _ in range(40):   # by name and atomic when `dst` is free: of two racing claimers of `src`, one gets FileNotFoundError
            try:
                os.rename(s, d)
                return
            except FileExistsError:
                break
            except PermissionError:   # src busy for a moment (another rename of it, a scanner), or dst exists
                if os.path.lexists(d):
                    break
                time.sleep(0.025)
        if not _posix_rename(s, d):
            _retry(os.replace, s, d)

    CLAIM_STALE = 120.0   # seconds after which a claim token is taken to be a crashed claimer's

    def claim_rename(src, dst, *, dir_fd) -> None:
        """Claim the record `src` by renaming it to `dst` (a name that differs per process): of
        several racing claimers exactly one succeeds, the rest get FileNotFoundError or
        FileExistsError.

        On Windows os.rename is not exclusive (concurrent renames of one file to different names
        can all report success, and so can hard links to different names), so the winner is
        decided by creating a token file with a name that is the same for every process,
        `<stem>.claim` (create-exclusive: it fails if it exists). Only the holder of the token
        renames `src`, then removes the token. A token left by a crashed holder (older than
        CLAIM_STALE) is removed by the next claimer, which then tries again."""
        token = src.rsplit(".", 1)[0] + ".claim"
        for attempt in (0, 1):
            try:
                fd = open(token, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=dir_fd)
            except FileExistsError:
                if attempt or not _stale_token(token, dir_fd):
                    raise
                continue
            except PermissionError as exc:   # the token is delete-pending (its holder just removed it)
                raise FileExistsError(errno.EEXIST, "claim token is being removed", token) from exc
            break
        os.close(fd)
        try:
            rename(src, dst, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)   # nobody else holds the token
        finally:
            try:
                unlink(token, dir_fd=dir_fd)
            except FileNotFoundError:
                pass

    def _stale_token(token: str, dir_fd) -> bool:
        """Remove `token` if it is older than CLAIM_STALE; True if it is gone now."""
        try:
            if time.time() - os.lstat(_path(token, dir_fd)).st_mtime < CLAIM_STALE:
                return False
            unlink(token, dir_fd=dir_fd)
        except FileNotFoundError:
            pass
        except OSError:
            return False
        return True

    def _posix_rename(src: str, dst: str) -> bool:
        """Rename with POSIX semantics (NTFS, Windows 10 1607+): replaces `dst` even while another
        handle has it open, as rename(2) does; os.replace fails with access denied then. False if
        this system or file system can't (the caller falls back to os.replace)."""
        import ctypes
        import struct
        from ctypes import wintypes
        if ctypes.sizeof(ctypes.c_void_p) != 8:
            return False
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        k32.SetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        full = os.path.abspath(dst)
        if not full.startswith("\\\\?\\"):
            full = "\\\\?\\" + full
        name = full.encode("utf-16-le")
        # FILE_RENAME_INFO_EX: Flags (REPLACE_IF_EXISTS | POSIX_SEMANTICS), RootDirectory, length, name
        buf = struct.pack("<I4xQI", 0x3, 0, len(name)) + name + b"\0\0"
        h = k32.CreateFileW(src, 0x10000, 7, None, 3, 0, None)   # DELETE access; open existing
        if h in (None, wintypes.HANDLE(-1).value):
            return False
        try:
            if k32.SetFileInformationByHandle(h, 22, buf, len(buf)):   # FileRenameInfoEx
                return True
            err = ctypes.get_last_error()
        finally:
            k32.CloseHandle(h)
        if err in (2, 3):   # the source is missing: the same error os.replace gives
            raise ctypes.WinError(err)
        return False

    def unlink(path, *, dir_fd=None) -> None:
        _retry(os.unlink, _path(path, dir_fd))

    def mkdir(path, mode=0o777, *, dir_fd=None) -> None:
        os.mkdir(_path(path, dir_fd))

    def link(src, dst, *, src_dir_fd=None, dst_dir_fd=None, follow_symlinks=True) -> None:
        os.link(_path(src, src_dir_fd), _path(dst, dst_dir_fd))

    def utime(path, times=None, *, dir_fd=None, follow_symlinks=True) -> None:
        os.utime(_path(path, dir_fd), times)

    def listdir(path=".") -> list[str]:
        if isinstance(path, int):
            path = _dirs[path]
        return os.listdir(path)

    def utime_fd(fd: int, name=None, dir_fd=None) -> None:
        # os.utime has no fd form on Windows: by name in the directory the file was opened from
        os.utime(_path(name, dir_fd), None)

    def replace(src, dst) -> None:
        """os.replace; a read-only destination (what chmod 0o400 makes on Windows) is made writable
        first, since POSIX lets a directory entry be replaced whatever the file's own mode."""
        try:
            os.replace(src, dst)
        except PermissionError:
            try:
                os.chmod(dst, _stat.S_IWRITE | _stat.S_IREAD)
            except OSError:
                raise
            _retry(os.replace, src, dst)

    def open_root(path) -> int:
        p = os.fspath(path)
        _need_dir(p)
        fd = os.open(os.devnull, os.O_RDONLY | os.O_BINARY)
        _dirs[fd] = p
        return fd

    def fsync_dir(fd: int) -> None:
        return None   # a directory cannot be flushed on Windows

    def popen_detach() -> dict:
        import subprocess
        return {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
                "encoding": "utf-8"}

    # --- file locking: LockFileEx on one byte at offset 2**40 (past any data, so a lock never
    # blocks a reader of the file's content; Windows locks are mandatory for the locked range).

    def flock(fd: int, op: int) -> None:
        import ctypes
        from ctypes import wintypes

        class _Overlapped(ctypes.Structure):
            _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                        ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD),
                        ("hEvent", wintypes.HANDLE)]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.LockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                   wintypes.DWORD, ctypes.POINTER(_Overlapped)]
        k32.UnlockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                     ctypes.POINTER(_Overlapped)]
        handle = msvcrt.get_osfhandle(fd if isinstance(fd, int) else fd.fileno())
        ov = _Overlapped()
        ov.OffsetHigh = 0x100
        if op & LOCK_UN:
            k32.UnlockFileEx(handle, 0, 1, 0, ctypes.byref(ov))   # not locked: harmless, like flock
            return
        flags = 0 if op & LOCK_SH else 2          # LOCKFILE_EXCLUSIVE_LOCK
        if op & LOCK_NB:
            flags |= 1                            # LOCKFILE_FAIL_IMMEDIATELY
        if not k32.LockFileEx(handle, flags, 0, 1, 0, ctypes.byref(ov)):
            err = ctypes.get_last_error()
            if err in (33, 32, 997):   # ERROR_LOCK_VIOLATION, ERROR_SHARING_VIOLATION, IO_PENDING
                raise BlockingIOError(errno.EAGAIN, "lock held by another process")
            raise ctypes.WinError(err)
