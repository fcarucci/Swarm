"""The orchestrating session's process, for the session driver (spec 3.11 [T21]): which process it is, and
waiting for it to exit without polling.

- `nearest_host(pid)`: the nearest claude/codex ancestor of `pid` (the hook or the `swarm activate` that starts a
  driver runs under its session's host process). Ancestry: /proc on Linux, `ps` on macOS, a Toolhelp32 snapshot
  on Windows. None when there is none or it cannot be read.
- `start_token(pid)`: what tells a process apart from a later one with the same pid (Linux: the start time in
  clock ticks, /proc/<pid>/stat field 22; macOS: `ps -o lstart`; Windows: the creation FILETIME). None when it
  cannot be read: a watch then treats the process as gone (fails closed: a driver never outlives a session it
  cannot vouch for).
- `ProcWatch(pid, token)`: `alive()` and `block()`, which returns when the process exits: a pidfd and poll(2)
  on Linux, kqueue EVFILT_PROC NOTE_EXIT on macOS/BSD, WaitForSingleObject on Windows. Only where none of these
  exists does `block()` look every few seconds (degraded)."""
from __future__ import annotations

import ctypes
import os
import subprocess
import time

from swarm import compat

POLL_SECONDS = 5.0


# --- ancestry ----------------------------------------------------------------------------------------

def _win_parents() -> dict:
    """{pid: (ppid, exe name)} of every process (CreateToolhelp32Snapshot). Empty when it cannot be read."""
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)       # TH32CS_SNAPPROCESS
    if snap in (None, wintypes.HANDLE(-1).value):
        return {}
    out = {}
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            out[int(entry.th32ProcessID)] = (int(entry.th32ParentProcessID), entry.szExeFile)
            ok = k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    return out


def parent_of(pid: int):
    """(ppid, argv) of `pid`, or None when it cannot be read. Windows gives the image name as argv[0]."""
    if compat.IS_WINDOWS:
        info = _win_parents().get(int(pid))
        return (info[0], [info[1]]) if info else None
    from swarm import hosts
    return hosts._proc_parent(int(pid))


def nearest_host(pid: int | None = None, parent=None, max_hops: int = 40) -> int | None:
    """The pid of the nearest claude/codex process at or above `pid`'s parent (default: this process's parent),
    or None (none found, or the ancestry cannot be read)."""
    from swarm import hosts
    parent = parent or parent_of
    pid = os.getppid() if pid is None else int(pid)
    for _ in range(max_hops):
        if pid <= (0 if compat.IS_WINDOWS else 1):
            return None
        try:
            info = parent(pid)
        except Exception:
            return None
        if not info:
            return None
        ppid, argv = info
        if hosts._classify(list(argv)):
            return pid
        if ppid == pid:
            return None
        pid = ppid
    return None


def ancestor_host(pid: int, parent=None, max_hops: int = 64) -> str | None:
    """"claude" or "codex" for the nearest agent host at or above `pid`, None when the walk reached the top
    (pid 1, or a parent that is gone: Windows keeps the pid of an exited parent) without one. Raises
    LookupError when `pid` itself cannot be read or the walk does not end: the caller fails closed."""
    from swarm import hosts
    parent = parent or parent_of
    pid = int(pid)
    for hop in range(max_hops):
        if pid <= (0 if compat.IS_WINDOWS else 1):
            return None
        info = parent(pid)
        if not info:
            if hop == 0:
                raise LookupError(f"the ancestry of process {pid} cannot be read")
            return None
        ppid, argv = info
        found = hosts._classify(list(argv))
        if found:
            return found
        if ppid == pid:
            return None
        pid = ppid
    raise LookupError("the ancestry is deeper than it can be walked")


def under_runner(pid: int, parent=None, max_hops: int = 64) -> bool:
    """Whether `pid` is a supervisor-launched seat or runs under one (review rr1): its environment carries the
    runner's RUN_ENV tag (where it can be read: /proc), or a `swarm.supervisor.runner` process is among its
    ancestors. A walk that cannot be read at all counts as yes (fails closed)."""
    from swarm.supervisor.runner import RUN_ENV
    try:
        with open(f"/proc/{int(pid)}/environ", "rb") as fh:
            if any(e.startswith(RUN_ENV.encode() + b"=") for e in fh.read().split(b"\0")):
                return True
    except OSError:
        pass
    parent = parent or parent_of
    cur = int(pid)
    for hop in range(max_hops):
        if cur <= (0 if compat.IS_WINDOWS else 1):
            return False
        info = parent(cur)
        if not info:
            return hop == 0
        ppid, argv = info
        if any("swarm.supervisor.runner" in str(a) for a in argv):
            return True
        if ppid == cur:
            return False
        cur = ppid
    return True


# --- identity ----------------------------------------------------------------------------------------

def _win_creation(pid: int) -> str | None:
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    h = k32.OpenProcess(0x1000, False, int(pid))      # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return None
    try:
        code = wintypes.DWORD(0)
        if not k32.GetExitCodeProcess(h, ctypes.byref(code)) or code.value != 259:   # STILL_ACTIVE
            return None                       # exited (a handle somewhere keeps its record): not a live process
        t = [wintypes.FILETIME() for _ in range(4)]
        if not k32.GetProcessTimes(h, *(ctypes.byref(x) for x in t)):
            return None
        return str((t[0].dwHighDateTime << 32) | t[0].dwLowDateTime)
    finally:
        k32.CloseHandle(h)


def start_token(pid: int) -> str | None:
    """A token that differs between two processes that had the same pid, or None when it cannot be read."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if compat.IS_WINDOWS:
        try:
            return _win_creation(pid)
        except Exception:
            return None
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
            return fh.read().rsplit(")", 1)[1].split()[19]
    except FileNotFoundError:
        if os.path.isdir("/proc/self"):
            return None                   # a /proc system: the process is gone
    except (OSError, ValueError, IndexError):
        return None
    try:
        out = subprocess.run([compat.ps_path(), "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    text = " ".join(out.stdout.split())
    return text if out.returncode == 0 and text else None


# --- the watch ---------------------------------------------------------------------------------------

class ProcWatch:
    """The session's host process. Pinned at construction where the OS allows (a pidfd, a process handle), so a
    later process with the same pid is never mistaken for it; the token is checked once pinned."""

    def __init__(self, pid: int, token: str | None) -> None:
        self.pid = int(pid)
        self.token = token
        self.closed = False
        self.kind = "poll"
        self._pidfd = None
        self._kq = None
        self._handle = None
        self._dead = False
        try:
            self._pin()
        except Exception:
            self._dead = True             # cannot pin a process that is already gone
        if not self._dead and (token is None or start_token(self.pid) != token):
            self._dead = True             # another process now has the pid (or it cannot be told): gone

    def _pin(self) -> None:
        if compat.IS_WINDOWS:
            from ctypes import wintypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.OpenProcess.restype = wintypes.HANDLE
            k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            h = k32.OpenProcess(0x00100000 | 0x1000, False, self.pid)   # SYNCHRONIZE | QUERY_LIMITED_INFORMATION
            if not h:
                raise OSError("OpenProcess failed")
            k32.CreateEventW.restype = wintypes.HANDLE
            k32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
            k32.WaitForMultipleObjects.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE), wintypes.BOOL,
                                                   wintypes.DWORD]
            k32.WaitForMultipleObjects.restype = wintypes.DWORD
            k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            k32.WaitForSingleObject.restype = wintypes.DWORD
            k32.SetEvent.argtypes = [wintypes.HANDLE]
            k32.CloseHandle.argtypes = [wintypes.HANDLE]
            self._stop = k32.CreateEventW(None, True, False, None)   # close() ends a block() at once
            self._handle, self._k32, self.kind = h, k32, "wait"
            return
        if hasattr(os, "pidfd_open"):
            try:
                self._pidfd = os.pidfd_open(self.pid)
                self.kind = "pidfd"
                return
            except ProcessLookupError:
                raise
            except OSError:
                self._pidfd = None        # an old kernel: try the next mechanism
        import select
        if hasattr(select, "kqueue"):
            kq = select.kqueue()
            ev = select.kevent(self.pid, filter=select.KQ_FILTER_PROC, flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                               fflags=select.KQ_NOTE_EXIT)
            kq.control([ev], 0)           # ESRCH (ProcessLookupError) when it is already gone
            self._kq, self.kind = kq, "kqueue"
            return
        os.kill(self.pid, 0)              # degraded: exists now; block() looks again every POLL_SECONDS

    def alive(self) -> bool:
        if self._dead:
            return False
        if self._pidfd is not None:
            import select
            p = select.poll()
            p.register(self._pidfd, select.POLLIN)
            if p.poll(0):
                self._dead = True
                return False
            return True
        if self._handle is not None:
            if self._k32.WaitForSingleObject(self._handle, 0) == 0:
                self._dead = True
                return False
            return True
        if self._kq is not None:
            if self._kq.control(None, 1, 0):
                self._dead = True
                return False
            return True
        try:
            os.kill(self.pid, 0)
        except ProcessLookupError:
            self._dead = True
            return False
        except PermissionError:
            pass
        if start_token(self.pid) != self.token:
            self._dead = True
            return False
        return True

    def block(self) -> bool:
        """Return True once the process has exited (blocking until then, without polling where possible). A
        one-shot source: once it has reported the exit it is closed (run_source does not call it again)."""
        if self._dead:
            self.closed = True
            return True
        if self._pidfd is not None:
            import select
            p = select.poll()
            p.register(self._pidfd, select.POLLIN)
            while not p.poll(None):
                pass
        elif self._handle is not None:
            from ctypes import wintypes
            handles = (wintypes.HANDLE * 2)(self._handle, self._stop)
            if self._k32.WaitForMultipleObjects(2, handles, False, 0xFFFFFFFF) != 0:
                return False                         # stopped by close(), not an exit
        elif self._kq is not None:
            while not self._kq.control(None, 1, None):
                pass
        else:
            while self.alive() and not self.closed:
                time.sleep(POLL_SECONDS)
        self._dead = True
        self.closed = True
        return True

    def close(self) -> None:
        self.closed = True
        if self._pidfd is not None:
            try:
                os.close(self._pidfd)
            except OSError:
                pass
        if self._kq is not None:
            try:
                self._kq.close()
            except OSError:
                pass
        if self._handle is not None:   # wake a blocked block() first; the handles go with the process
            try:
                self._k32.SetEvent(self._stop)
            except Exception:
                pass


class NoWatch:
    """`swarm drive run`: no session process to watch (it is never run as a source)."""
    kind = "none"
    closed = False

    def alive(self) -> bool:
        return True

    def close(self) -> None:
        self.closed = True
