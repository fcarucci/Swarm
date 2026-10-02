"""Automatic initialisation: every CLI command and hook that opens the board first makes sure
its storage is set up for this code (`ensure_initialized`), so `swarm init` is never required.

The store records the schema version its last setup installed (base.SCHEMA_VERSION; see each
backend's `schema_version`). On open:

* a local stamp file `<host>/schema-<backend>-<store identity>-<SCHEMA_VERSION>` exists: done,
  without touching the store (the common case: a stat, no query);
* otherwise the store's version is read. Missing or older: the backend's `setup` (the same
  idempotent one `swarm init` runs: create the store, schema and migrations, name pool) runs
  under a lock (Postgres: an advisory lock, across machines; the others: a lock file here), the
  version is re-read under the lock first so concurrent opens migrate once. Newer than this code:
  left alone, reported as "newer" for the caller to warn about. Then the stamp is written, so a
  store is checked once per machine and code version.

Stores that don't outlive the process (memory) have no stamp: they are checked every time.
`SWARM_AUTO_INIT=0` in the environment turns all of it off (the test suite does, except where it
tests this module).

The stamps and the lock file live in the host-only dir (paths.host_dir(): 0700, outside every
sandbox writable root) and are only ever reached through swarm.safefs: a
symlink, hard link or FIFO planted there is refused, never followed or waited on.
"""
from __future__ import annotations

import contextlib
from swarm import compat
import hashlib
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from .base import SCHEMA_VERSION, SetupResult, load_name_pool

_LOCAL_LOCKS: dict[str, threading.Lock] = {}
_LOCAL_LOCKS_GUARD = threading.Lock()


@dataclass(frozen=True)
class InitResult:
    """What ensure_initialized did. action: "disabled" (SWARM_AUTO_INIT=0), "stamped" (the stamp
    said it was done: nothing read), "current" (the store already had this version),
    "initialized" (setup ran; `setup` is its result), "newer" (the store records a newer version
    than this code: untouched). version: the store's version as read (None if not read, or no
    store yet)."""
    action: str
    version: int | None = None
    setup: SetupResult | None = None


def enabled() -> bool:
    return os.environ.get("SWARM_AUTO_INIT", "1").strip() != "0"


def state_dir() -> Path:
    """Where the stamps and the lock file live: the host-only dir (the name is historical)."""
    from swarm import paths
    return paths.host_dir()


def _backend_name(cfg: dict) -> str:
    from swarm.board import board_backend
    return board_backend(cfg)


def _safe(identity: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9.-]+", "_", identity).strip("_")
    if len(safe) > 120:
        safe = safe[:100] + "-" + hashlib.sha1(identity.encode()).hexdigest()[:12]
    return safe


def store_key(cfg: dict) -> str:
    """A file-name-safe key for the configured store: "<backend>-<identity>" (a memory store:
    its name). For per-board state files next to the stamps (transcript snapshots)."""
    from . import backend_class
    identity = backend_class(cfg).identity(cfg)
    if identity is None:
        identity = str((cfg.get("memory") or {}).get("store", "default"))
    return f"{_backend_name(cfg)}-{_safe(identity)}"


def stamp_path(cfg: dict) -> Path | None:
    """This store's stamp for SCHEMA_VERSION, or None when the backend keeps no stamps."""
    from . import backend_class
    identity = backend_class(cfg).identity(cfg)
    if identity is None:
        return None
    return state_dir() / f"schema-{_backend_name(cfg)}-{_safe(identity)}-{SCHEMA_VERSION}"


def _host_fd(create: bool) -> int:
    from swarm import safefs
    return safefs.open_base(state_dir(), create=create, strict_mode=0o700)


def _stamped(path: Path) -> bool:
    """Whether the stamp is there: a private regular file (a planted link is no stamp)."""
    from swarm import safefs
    try:
        d = _host_fd(create=False)
    except (OSError, ValueError):
        return False
    try:
        os.close(safefs.open_existing(d, path.name, os.O_RDONLY))
        return True
    except OSError:
        return False
    finally:
        os.close(d)


def _write_stamp(path: Path | None) -> None:
    if path is None:
        return
    from swarm import safefs
    try:
        d = _host_fd(create=True)
        try:
            safefs.touch(d, path.name)
        finally:
            os.close(d)
    except (OSError, ValueError):
        pass   # e.g. a sandbox: the store is checked again next time, which is only slower


@contextlib.contextmanager
def _local_lock(cfg: dict, cls, timeout: float):
    """A lock file next to the stamps (sqlite, file: one machine), or an in-process lock for
    stores without an identity (memory)."""
    identity = cls.identity(cfg)
    if identity is None:
        with _LOCAL_LOCKS_GUARD:
            lock = _LOCAL_LOCKS.setdefault(_backend_name(cfg), threading.Lock())
        if not lock.acquire(timeout=max(0.0, timeout)):
            raise TimeoutError("another swarm setup is running")
        try:
            yield
        finally:
            lock.release()
        return
    from swarm import safefs
    name = f"schema-{_backend_name(cfg)}-{_safe(identity)}.lock"
    d = _host_fd(create=True)
    try:
        fd = safefs.open_lock(d, name)   # never follows a link, never blocks on a FIFO
    finally:
        os.close(d)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                compat.flock(fd, compat.LOCK_EX | compat.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"another swarm setup holds {state_dir() / name}") from None
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)   # releases the flock


def setup_lock(cfg: dict, timeout: float):
    """The lock setups of this store run under (see the module docstring)."""
    from . import backend_class
    cls = backend_class(cfg)
    backend_lock = getattr(cls, "setup_lock", None)
    return backend_lock(cfg, timeout) if backend_lock else _local_lock(cfg, cls, timeout)


def _classify(version: int | None) -> str | None:
    if version is None or version < SCHEMA_VERSION:
        return None
    return "current" if version == SCHEMA_VERSION else "newer"


def ensure_initialized(cfg: dict, timeout: float = 60.0,
                       names: Mapping[str, Sequence[str]] | None = None) -> InitResult:
    """Set the configured store up if it isn't (for this code's SCHEMA_VERSION); see the module
    docstring. timeout bounds the wait for another process's setup (TimeoutError). Raises what
    the backend raises (BoardUnavailable when unreachable, IncompatibleStorage)."""
    if not enabled():
        return InitResult("disabled")
    from . import backend_class
    cls = backend_class(cfg)
    stamp = stamp_path(cfg)
    if stamp is not None and _stamped(stamp):
        if not cls.store_missing(cfg):
            return InitResult("stamped")
        _drop_stamp(stamp)   # the store went away behind it (a deleted file or directory)
    version = cls.schema_version(cfg)
    action = _classify(version)
    result = None
    if action is None:
        with setup_lock(cfg, timeout):
            version = cls.schema_version(cfg)   # another process may have just done it
            action = _classify(version)
            if action is None:
                result = cls.setup(cfg, load_name_pool() if names is None else names)
                action = "initialized"
    _write_stamp(stamp)
    return InitResult(action, version, result)


def _drop_stamp(stamp: Path | None) -> None:
    if stamp is None:
        return
    from swarm import safefs
    try:
        d = _host_fd(create=False)
    except (OSError, ValueError):
        return
    try:
        safefs.unlink(d, stamp.name)
    except OSError:
        pass
    finally:
        os.close(d)


def recover_missing(cfg: dict, timeout: float = 60.0) -> bool:
    """After opening the board failed: if the store itself is gone (Board.store_missing, which
    may ask the server), drop the stale stamp and set the store up again; True if it did, so
    the caller retries the open once. False (the caller re-raises) for anything else: a mere
    connection error never re-initialises."""
    if not enabled():
        return False
    from . import backend_class
    if not backend_class(cfg).store_missing(cfg, cheap=False):
        return False
    _drop_stamp(stamp_path(cfg))
    ensure_initialized(cfg, timeout)
    return True


def initialize(cfg: dict, names: Mapping[str, Sequence[str]] | None = None,
               timeout: float = 60.0) -> SetupResult:
    """`swarm init`: run setup unconditionally (it also re-bakes config into the views), under
    the setup lock, and stamp the store."""
    from . import backend_class
    cls = backend_class(cfg)
    with setup_lock(cfg, timeout):
        result = cls.setup(cfg, load_name_pool() if names is None else names)
    if enabled():
        _write_stamp(stamp_path(cfg))
    return result
