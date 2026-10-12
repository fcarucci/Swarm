"""Wake files and change notification for the session driver (spec 3.11 [T21]).

The session driver sleeps until something happens; nothing here polls while a notification mechanism exists.

- **Wake file.** One per board: `<private dir>/wake/<sha256(board_key)>` (~/.local/share/swarm/supervisor/wake/).
  `touch(board_key)` sets its times to now (creating it 0600 if missing). Every swarm CLI board write, every hook
  board write, every runner exit and every kick (`drivegate.kick`) touches it. Touching only wakes a pass, which
  decides from the ledger and the board alone, so a planted or spurious touch costs one pass and nothing else.
  Nothing is touched on a host that has never activated a drive job (no `supervisor/drive` directory).
- **Notification.** `file_source(board_key)` watches the wake file with the OS: inotify (Linux, ctypes),
  kqueue EVFILT_VNODE (macOS/BSD, `select.kqueue`), ReadDirectoryChangesW (Windows, ctypes). Each source blocks
  in its own daemon thread (no CPU while idle) and signals a `Hub`. A board that pushes changes (Postgres
  LISTEN/NOTIFY, the in-process memory board) gets a `board_source` too.
- **Fallback.** Only when no OS notification is available does `PollSource` look at the wake file's mtime, on a
  backed-off schedule (`Backoff`: 2 s doubling to 60 s while nothing changes, back to 2 s on a change).

`Hub.wait(timeout)` is one blocking wait on a condition (a futex on Linux), woken by a source or by the timeout
the caller computed from the ledger deadlines: there is no periodic tick."""
from __future__ import annotations

import ctypes
import hashlib
import logging
import os
import struct
import sys
import threading
import time

from swarm import compat

log = logging.getLogger("swarm.supervisor.wake")

SUBDIR = "wake"
POLL_MIN = 2.0
POLL_MAX = 60.0
CHANGE, EXIT, ERROR = "change", "exit", "error"


# --- the wake file ---------------------------------------------------------------------------------

def wake_name(board_key: str) -> str:
    return hashlib.sha256(str(board_key).encode("utf-8", "surrogatepass")).hexdigest()


def wake_dir():
    from swarm.supervisor.settings import private_dir
    return private_dir() / SUBDIR


def _drive_host() -> bool:
    """Whether this host has a drive ledger directory (a drive job was ever activated here)."""
    from swarm.supervisor import ledger
    from swarm.supervisor.settings import private_dir
    return os.path.isdir(private_dir() / ledger.SUBDIR)


def touch(board_key: str) -> bool:
    """Touch the board's wake file (created 0600 if missing, through the verified private directory, never
    through a link). True when touched; never raises. A host without drive ledgers touches nothing."""
    try:
        if not board_key or not _drive_host():
            return False
        from swarm import safefs
        from swarm.supervisor import privfs
        with privfs.dir_fd(SUBDIR) as d:
            safefs.touch(d, wake_name(board_key))
        return True
    except Exception as exc:   # a sandbox without the private dir, a planted entry: no wake, no failure
        log.debug("wake touch failed: %s", type(exc).__name__)
        return False


def touch_cfg(cfg: dict) -> bool:
    """touch() for the board of `cfg` (its store key). Never raises."""
    try:
        if not _drive_host():
            return False
        from swarm.board.autoinit import store_key
        return touch(store_key(cfg))
    except Exception:
        return False


def touch_all() -> bool:
    """Touch every wake file of this host (a kick that does not know its board). Never raises."""
    try:
        if not _drive_host():
            return False
        from swarm import safefs
        from swarm.supervisor import privfs
        done = False
        with privfs.dir_fd(SUBDIR) as d:
            for name in compat.listdir(d):
                if len(name) == 64 and all(c in "0123456789abcdef" for c in name):
                    try:
                        safefs.touch(d, name)
                        done = True
                    except OSError:
                        continue
        return done
    except Exception:
        return False


def ensure(board_key: str) -> str:
    """Create the wake directory and file if missing; the file's path (what the sources watch)."""
    from swarm import safefs
    from swarm.supervisor import privfs
    with privfs.dir_fd(SUBDIR) as d:
        try:
            fd = safefs.create(d, wake_name(board_key))
            os.close(fd)
        except FileExistsError:
            pass
    return str(wake_dir() / wake_name(board_key))


def mtime(board_key: str) -> float | None:
    try:
        return os.stat(wake_dir() / wake_name(board_key), follow_symlinks=False).st_mtime_ns / 1e9
    except OSError:
        return None


# --- the hub -----------------------------------------------------------------------------------------

class Hub:
    """Where sources report. `wait(timeout)` blocks (no polling) until a reason is signalled or the timeout
    passes, and returns the reasons signalled since the previous wait (an empty set on a timeout)."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._reasons: set = set()
        self.wakeups = 0           # how many times wait() returned (tests count them)

    def signal(self, reason: str) -> None:
        with self._cond:
            self._reasons.add(reason)
            self._cond.notify_all()

    def wait(self, timeout: float | None) -> set:
        with self._cond:
            if not self._reasons:
                if timeout is None:
                    self._cond.wait()
                elif timeout > 0:
                    self._cond.wait(min(timeout, threading.TIMEOUT_MAX))
            out, self._reasons = self._reasons, set()
            self.wakeups += 1
            return out


def run_source(hub: Hub, source, reason: str = CHANGE, name: str = "wake-source") -> threading.Thread:
    """Run `source.block()` in a daemon thread, forever: each return is one `reason` for the hub. A source that
    raises signals ERROR once and stops (the driver then falls back to polling)."""

    def loop():
        try:
            while not getattr(source, "closed", False):
                if source.block():
                    hub.signal(reason)
        except Exception as exc:
            if not getattr(source, "closed", False):
                log.warning("%s failed: %s", name, type(exc).__name__)
                hub.signal(ERROR)

    t = threading.Thread(target=loop, name=name, daemon=True)
    t.start()
    return t


# --- the poll fallback -------------------------------------------------------------------------------

class Backoff:
    """The degraded poll's schedule: POLL_MIN doubling to POLL_MAX while nothing changes, POLL_MIN again after
    a change."""

    def __init__(self, low: float = POLL_MIN, high: float = POLL_MAX) -> None:
        self.low, self.high, self.current = low, high, low

    def next(self, changed: bool) -> float:
        """The delay before the next look, given whether the previous look saw a change."""
        if changed:
            self.current = self.low
        else:
            self.current = min(self.high, self.current * 2)
        return self.current


class PollSource:
    """Looks at the wake file's mtime on the Backoff schedule (degraded mode). block() returns True on a
    change. `sleep` and `stat` are injectable."""
    kind = "poll"

    def __init__(self, board_key: str, *, sleep=time.sleep, stat=None) -> None:
        self.board_key = board_key
        self.sleep = sleep
        self.stat = stat or (lambda: mtime(board_key))
        self.backoff = Backoff()
        self.delay = self.backoff.low
        self.seen = self.stat()
        self.closed = False

    def block(self) -> bool:
        while not self.closed:
            self.sleep(self.delay)
            now = self.stat()
            changed = now != self.seen
            self.seen = now
            self.delay = self.backoff.next(changed)
            if changed:
                return True
        return False

    def close(self) -> None:
        self.closed = True


# --- Linux: inotify ----------------------------------------------------------------------------------

IN_MODIFY, IN_ATTRIB, IN_CLOSE_WRITE, IN_MOVED_TO, IN_CREATE = 0x2, 0x4, 0x8, 0x80, 0x100
IN_DELETE_SELF, IN_MOVE_SELF, IN_IGNORED = 0x400, 0x800, 0x8000
IN_ONLYDIR, IN_DONT_FOLLOW, IN_CLOEXEC = 0x01000000, 0x02000000, 0o2000000
_EVENT = struct.Struct("iIII")


class InotifySource:
    """inotify on the wake directory, filtered to this board's wake file (its creation, a touch, a rename onto
    it). The watch is added with IN_DONT_FOLLOW | IN_ONLYDIR: a link planted for the directory is refused."""
    kind = "notify"

    def __init__(self, board_key: str) -> None:
        self.name = wake_name(board_key).encode()
        ensure(board_key)
        libc = ctypes.CDLL(None, use_errno=True)
        init1 = getattr(libc, "inotify_init1")
        add = getattr(libc, "inotify_add_watch")
        add.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        self.fd = init1(IN_CLOEXEC)
        if self.fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1 failed")
        mask = (IN_MODIFY | IN_ATTRIB | IN_CLOSE_WRITE | IN_MOVED_TO | IN_CREATE | IN_DELETE_SELF | IN_MOVE_SELF
                | IN_ONLYDIR | IN_DONT_FOLLOW)
        if add(self.fd, os.fsencode(str(wake_dir())), mask) < 0:
            err = ctypes.get_errno()
            os.close(self.fd)
            raise OSError(err, "inotify_add_watch failed")
        self.closed = False

    def block(self) -> bool:
        data = os.read(self.fd, 64 * 1024)
        hit = False
        i = 0
        while i + _EVENT.size <= len(data):
            _wd, mask, _cookie, length = _EVENT.unpack_from(data, i)
            name = data[i + _EVENT.size:i + _EVENT.size + length].rstrip(b"\0")
            i += _EVENT.size + length
            if mask & (IN_IGNORED | IN_DELETE_SELF | IN_MOVE_SELF):
                raise OSError("the wake directory went away")
            if name == self.name:
                hit = True
        return hit

    def close(self) -> None:
        self.closed = True
        try:
            os.close(self.fd)
        except OSError:
            pass


# --- macOS / BSD: kqueue -----------------------------------------------------------------------------

class KqueueSource:
    """kqueue EVFILT_VNODE on the wake file itself (a touch is NOTE_ATTRIB); reopened after a delete or a
    rename. O_EVTONLY on macOS, O_NOFOLLOW always."""
    kind = "notify"

    def __init__(self, board_key: str) -> None:
        import select
        self.select = select
        self.board_key = board_key
        self.kq = select.kqueue()
        self.fd = -1
        self._arm()
        self.closed = False

    def _arm(self) -> None:
        sel = self.select
        if self.fd >= 0:
            os.close(self.fd)
        path = ensure(self.board_key)
        flags = os.O_RDONLY | getattr(os, "O_EVTONLY", 0x8000 if sys.platform == "darwin" else 0) | os.O_NOFOLLOW
        self.fd = os.open(path, flags)
        ev = sel.kevent(self.fd, filter=sel.KQ_FILTER_VNODE, flags=sel.KQ_EV_ADD | sel.KQ_EV_CLEAR,
                        fflags=(sel.KQ_NOTE_ATTRIB | sel.KQ_NOTE_WRITE | sel.KQ_NOTE_EXTEND | sel.KQ_NOTE_DELETE
                                | sel.KQ_NOTE_RENAME))
        self.kq.control([ev], 0)

    def block(self) -> bool:
        sel = self.select
        events = self.kq.control(None, 4, None)
        if any(e.fflags & (sel.KQ_NOTE_DELETE | sel.KQ_NOTE_RENAME) for e in events):
            self._arm()
        return bool(events)

    def close(self) -> None:
        self.closed = True
        for closer in (lambda: os.close(self.fd), self.kq.close):
            try:
                closer()
            except OSError:
                pass


# --- Windows: ReadDirectoryChangesW ------------------------------------------------------------------

def _win_k32():
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    k32.CreateEventW.restype = wintypes.HANDLE
    k32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    k32.SetEvent.argtypes = [wintypes.HANDLE]
    k32.ResetEvent.argtypes = [wintypes.HANDLE]
    k32.ReadDirectoryChangesW.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, wintypes.BOOL,
                                          wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
                                          ctypes.c_void_p]
    k32.WaitForMultipleObjects.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE), wintypes.BOOL,
                                           wintypes.DWORD]
    k32.WaitForMultipleObjects.restype = wintypes.DWORD
    k32.GetOverlappedResult.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD),
                                        wintypes.BOOL]
    k32.CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    return k32


class WinDirSource:
    """ReadDirectoryChangesW on the wake directory, overlapped, filtered to this board's wake file (a touch
    changes its last-write time). The wait is on the I/O's event and a stop event, so close() never blocks
    (closing a handle with synchronous I/O pending in another thread would wait for that I/O)."""
    kind = "notify"

    def __init__(self, board_key: str) -> None:
        import ctypes
        from ctypes import wintypes
        self.ct, self.wt = ctypes, wintypes
        self.name = wake_name(board_key)
        ensure(board_key)
        k32 = self.k32 = _win_k32()

        class OVERLAPPED(ctypes.Structure):
            _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                        ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD), ("hEvent", wintypes.HANDLE)]
        self.OVERLAPPED = OVERLAPPED
        # FILE_LIST_DIRECTORY; share read/write/delete; OPEN_EXISTING; FILE_FLAG_BACKUP_SEMANTICS (a directory)
        # | FILE_FLAG_OPEN_REPARSE_POINT (a junction planted for the directory is not followed) | OVERLAPPED
        h = k32.CreateFileW(str(wake_dir()), 0x1, 0x7, None, 3, 0x02000000 | 0x00200000 | 0x40000000, None)
        if h in (None, wintypes.HANDLE(-1).value):
            raise ctypes.WinError(ctypes.get_last_error())
        self.h = h
        self.io = k32.CreateEventW(None, True, False, None)
        self.stop = k32.CreateEventW(None, True, False, None)
        self.buf = ctypes.create_string_buffer(64 * 1024)
        self.closed = False

    def block(self) -> bool:
        ct, wt, k32 = self.ct, self.wt, self.k32
        ov = self.OVERLAPPED()
        ov.hEvent = self.io
        k32.ResetEvent(self.io)
        n = wt.DWORD(0)
        # FILE_NOTIFY_CHANGE_FILE_NAME | ATTRIBUTES | LAST_WRITE
        if not k32.ReadDirectoryChangesW(self.h, self.buf, len(self.buf), False, 0x1 | 0x4 | 0x10, None,
                                         ct.byref(ov), None):
            raise ct.WinError(ct.get_last_error())
        handles = (wt.HANDLE * 2)(self.io, self.stop)
        which = k32.WaitForMultipleObjects(2, handles, False, 0xFFFFFFFF)
        if which != 0:                               # the stop event (or a failed wait): cancel and end
            k32.CancelIoEx(self.h, ct.byref(ov))
            k32.GetOverlappedResult(self.h, ct.byref(ov), ct.byref(n), True)
            self._release()
            return False
        if not k32.GetOverlappedResult(self.h, ct.byref(ov), ct.byref(n), False):
            raise ct.WinError(ct.get_last_error())
        if n.value == 0:
            return True            # the buffer overflowed: something changed, look anyway
        raw, i, hit = self.buf.raw[:n.value], 0, False
        while True:
            nxt, _action, length = struct.unpack_from("<III", raw, i)
            name = raw[i + 12:i + 12 + length].decode("utf-16-le", "replace")
            if name == self.name:
                hit = True
            if not nxt:
                return hit
            i += nxt

    def _release(self) -> None:
        for h in (self.h, self.io, self.stop):
            try:
                self.k32.CloseHandle(h)
            except Exception:
                pass

    def close(self) -> None:
        self.closed = True
        try:
            self.k32.SetEvent(self.stop)     # the blocked thread cancels its read, closes the handles and ends
        except Exception:
            pass


def file_source(board_key: str):
    """The OS notification source for the board's wake file, or None when this platform has none that works
    (the caller then polls, and says so)."""
    candidates = []
    if sys.platform.startswith("linux"):
        candidates.append(InotifySource)
    elif compat.IS_WINDOWS:
        candidates.append(WinDirSource)
    elif hasattr(__import__("select"), "kqueue"):
        candidates.append(KqueueSource)
    for cls in candidates:
        try:
            return cls(board_key)
        except Exception as exc:
            log.warning("no %s notification: %s", cls.__name__, exc)
    return None


# --- boards that push --------------------------------------------------------------------------------

class BoardSource:
    """A board that pushes changes (Postgres LISTEN/NOTIFY, the in-process memory board): block() is its
    wait_for_change on a connection of its own. Only used while the board says "push"."""
    kind = "listen"

    def __init__(self, board, block_seconds: float = 3600.0) -> None:
        self.board = board
        self.block_seconds = block_seconds
        board.subscribe(False)
        self.closed = False

    def block(self) -> bool:
        return bool(self.board.wait_for_change(self.block_seconds))

    def close(self) -> None:
        self.closed = True


def board_pushes(board) -> bool:
    try:
        return bool(getattr(board, "supports_subscribe", False)) and board.change_mode == "push"
    except Exception:
        return False
