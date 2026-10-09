"""Local-file backend for the swarm board: the board in plain files, no database.

Select it with `[board] backend = "file"`; `[file] path` is the board directory (default
`~/.local/share/swarm-board/board`, outside every sandbox's writable roots; created on first
use). It coordinates every process on ONE
machine (all the hooks, CLIs and `watch`es); it is not for boards shared between hosts, and the
directory must be on a local filesystem (flock over NFS/SMB is not dependable).

Semantics are not re-implemented here. `FileBoard` IS a `MemoryBoard` whose store, a
`FileStore`, keeps its rows on disk: every `with store.lock:` block of the memory backend
becomes one transaction that

  1. takes an exclusive `compat.flock` on `<path>/lock` (plus a thread lock, for threads that
     share a store);
  2. loads the rows from disk (messages lazily: only when the block touches them);
  3. runs the memory backend's code unchanged over them;
  4. persists what changed, then unlocks. A block that raises persists nothing.

So every method is exactly as atomic as it is in the memory backend (where the same block runs
under the store's in-process lock), now across processes.

Files in the directory:

* `lock`: the flock target; empty, never replaced or deleted.
* `schema_version`: the schema version setup recorded (base.SCHEMA_VERSION), one integer.
* `state.json`: pool, jobs, agents, routes, restarts and the next ids, one JSON document. Rewritten
  whole when it changed: write a fresh temp file with a random name (O_EXCL), fsync, rename it
  over `state.json` relative to the directory's descriptor, fsync the directory. A
  reader sees the old or the new document, never a mix; a crash leaves one of the two.
* `transcripts/index.json`: the transcript rows without bodies (loaded only by transcript
  methods), replaced like `state.json`; `transcripts/<sha1 of job and agent_key>.xz`: each
  body (compressed), written atomically before the index that refers to it;
  `transcripts/images/<sha256>`: each transcript image, raw, once (the index rows hold the
  references and metadata; an image goes when no row refers to it any more).
* `transcripts/memory_refs.json` (schema 8, memory provenance): the memory refs without their
  excerpts (loaded only by the memory-ref and image methods), replaced like `state.json`; each
  excerpt is a body file like a transcript's, under the key `memory\x1f<document_id>`, written
  before the index. Their images are the transcript images above (an image goes when neither a
  transcript nor a memory ref refers to it).
* `messages.jsonl`: one message per line, in id order. New messages are appended in one
  `write` and fsynced BEFORE the state (which holds next_id) is replaced, so a crash in between
  cannot reuse an id: on load next_id is max(stored next_id, last message id + 1). A crash
  mid-append can leave only a torn last line without its newline; the next transaction that
  loads the messages under the lock truncates it (that post was never acknowledged). Retention
  (and anything else that changes an old line) rewrites the file like `state.json`.

Ids are assigned under the lock, so they are unique and strictly increasing, and a message is
visible exactly when its transaction ends, after every message with a smaller id: a cursor
reader never skips one.

Change detection (`watch`, `tail`): `wait_for_change` polls the (inode, mtime, size) of
`messages.jsonl` (and of `state.json` unless messages_only) every POLL_SECONDS; an empty
`messages.jsonl` counts as absent, so the first post (create, then append) is one change.
Transactions that change nothing write nothing, so reads never wake watchers.

Scale: every transaction reads the state, and those that touch messages read all of them; a
change rewrites the state. That is fine at the sizes the board has in practice (hundreds of
messages a week under 7-day retention, dozens of agents: well under a megabyte): measured with
500 messages and 30 agents, one tool call's hooks (PreToolUse + PostToolUse) take about 20 ms,
mostly fsync. Tens of thousands of retained messages would make every hook slower;
use Postgres (or shorten retention_days) for that.

Planted files: the directory is opened once through
safefs.open_base (no symlinked component, this user's, not group- or world-writable) and every
file is opened relative to that descriptor with O_NOFOLLOW | O_NONBLOCK and checked to be a
regular file of this user with one link, at most READ_LIMIT bytes. A symlink (even dangling),
hard link or FIFO at `lock`, `state.json`, `messages.jsonl` or `transcripts/index.json` makes the
board BoardUnavailable; nothing is ever written, truncated or read through one (a planted
transcript body or image reads as missing).

Sandboxes: an agent whose sandbox can't write the directory can't open the board: construction
raises BoardUnavailable (from the OSError), so `swarm post` spools to `[board] spool_dir` and the
hooks, which run outside the sandbox, deliver it, exactly as with an unreachable Postgres.

Read-only (`open_read_only`, `swarm supervise --dry-run`): `FileBoard(cfg, read_only=True)`
creates, writes, truncates and renames nothing. The directory must exist (else
BoardUnavailable); the lock file is opened O_RDONLY and flocked shared, or not at all if it is
missing; a missing state.json reads as empty; a torn last message line is skipped, not cut. Every
write method raises ReadOnlyBoard, and so does a transaction that would have written anything.

Test hook (not part of the Board interface): `set_available(path, False)` makes construction
for that directory raise BoardUnavailable from ConnectionError, like the memory backend's flag.
"""
from __future__ import annotations

import contextlib
import datetime as _dt
from swarm import compat
import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path

from .. import safefs

from .base import (SCHEMA_VERSION, Board, SHA256_RULE, BoardError, BoardUnavailable, NAME_SOURCES, ReadOnlyBoard,
                   SetupResult, refuse_writes)
from .memory import MemoryBoard, MemoryStore

FORMAT = 1               # state.json "format"; a newer one is refused, never overwritten
POLL_SECONDS = 0.1       # wait_for_change polling interval
DEFAULT_PATH = "~/.local/share/swarm-board/board"

_STATE, _MESSAGES, _LOCK = "state.json", "messages.jsonl", "lock"
_TRANSCRIPTS, _TRANSCRIPT_INDEX, _IMAGES = "transcripts", "index.json", "images"
_MEMORY_REFS = "memory_refs.json"   # in _TRANSCRIPTS
_VERSION = "schema_version"   # base.SCHEMA_VERSION as text, written by setup
_UNAVAILABLE: set[str] = set()


def board_dir(cfg: dict) -> Path:
    return Path((cfg.get("file") or {}).get("path") or DEFAULT_PATH).expanduser()


def set_available(path, available: bool = True) -> None:
    """Test hook: mark the board directory unavailable (construction raises BoardUnavailable
    from ConnectionError) or available again. In-process only."""
    key = str(Path(path).expanduser())
    (_UNAVAILABLE.discard if available else _UNAVAILABLE.add)(key)


# ---- JSON with datetimes and tuples --------------------------------------------------

def _enc(v):
    if isinstance(v, _dt.datetime):
        return {"$dt": v.isoformat()}
    if isinstance(v, tuple):
        return {"$t": [_enc(x) for x in v]}
    if isinstance(v, list):
        return [_enc(x) for x in v]
    if isinstance(v, dict):
        return {k: _enc(x) for k, x in v.items()}
    return v


def _dec(d: dict):
    if len(d) == 1:
        if "$dt" in d:
            return _dt.datetime.fromisoformat(d["$dt"])
        if "$t" in d:
            return tuple(d["$t"])
    return d


def dumps(v) -> str:
    return json.dumps(_enc(v), ensure_ascii=False, separators=(",", ":"))


def loads(text: str):
    return json.loads(text, object_hook=_dec)


# ---- file access through the board directory's descriptor ---------------------------------
#
# The board directory may be writable by a sandboxed agent (the old default was inside the Codex
# writable roots), while the board is written by host-side processes outside the sandbox. So no
# board file is ever opened by name: the directory is opened once (safefs.open_base: no symlinked
# component, this user's, not group/world-writable) and every file is opened relative to that
# descriptor through safefs: O_NOFOLLOW | O_NONBLOCK, then checked to be a regular file of this
# user with one link. A planted symlink, hard link or FIFO is refused (BoardUnavailable), never
# followed, truncated or waited on; writes are fresh O_EXCL temp files with random names renamed
# over the name (safefs.write_atomic), so nothing is ever written through a planted entry.

READ_LIMIT = 1 << 30   # largest board file a load reads (a sandbox can plant a huge one)


def _refused(name: str, why: str = "not a private regular file of this user (a planted link?)"):
    msg = f"file board: {name} is {why}: refusing to use it"
    return BoardUnavailable(msg)


def _read(d: int, name: str, *, what: str | None = None, strict: bool = True) -> bytes | None:
    """The content of `name` in directory `d`; None if there is no entry. Anything there that
    safefs.read refuses (a symlink, even dangling; a hard link, FIFO, directory, another user's
    file, over READ_LIMIT) raises BoardUnavailable, or reads as missing when not `strict` (a
    transcript body or image: one planted file must not break every transcript read)."""
    if not safefs.exists(d, name):
        return None
    data = safefs.read(d, name, READ_LIMIT)
    if data is None:
        if not strict or not safefs.exists(d, name):   # refused, or removed in between
            return None
        err = _refused(what or name)
        raise err from safefs.UnsafePathError(str(err))
    return data


def _write(d: int, name: str, data: bytes) -> None:
    """Replace `name` in `d` (fresh O_EXCL temp file, fsync, rename relative to `d`, fsync `d`)."""
    safefs.write_atomic(d, name, data)


def _append(d: int, name: str, data: bytes) -> None:
    """Append `data` to `name` in `d` in one O_APPEND write loop, fsynced; created 0600 (O_EXCL)
    if missing. A planted link or FIFO raises BoardUnavailable and nothing is written."""
    created = False
    try:
        try:
            fd, created = safefs.create(d, name), True
        except FileExistsError:
            fd = safefs.open_existing(d, name, os.O_WRONLY | os.O_APPEND)
    except OSError as exc:
        raise _refused(name) from exc
    try:
        if created:
            compat.fchmod(fd, 0o600)
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)
    if created:
        compat.fsync_dir(d)


def _sub(d: int, name: str, *, create: bool) -> int | None:
    """A verified descriptor of subdirectory `name` of `d` (created 0700 if `create`), or None if
    it is missing and not `create`. A symlink or another user's directory: BoardUnavailable."""
    try:
        return safefs.open_sub(d, name, create=create)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _refused(name, "not a private directory of this user (a planted link?)") from exc


@contextlib.contextmanager
def _open_dir(path: Path, *, create: bool):
    """The board directory's verified descriptor for the block (BoardUnavailable if unsafe)."""
    try:
        fd = safefs.open_base(path, create=create)
    except FileNotFoundError:
        raise
    except (OSError, ValueError) as exc:
        raise BoardUnavailable(str(exc)) from exc
    try:
        yield fd
    finally:
        os.close(fd)


# ---- the memory refs index: checked on load --------------------------------------------------
#
# transcripts/memory_refs.json may be rewritten by anything that can write the board directory
# (a sandboxed agent, where the directory is inside its writable roots). The memory backend's
# code indexes its rows without checks, so a missing key or a wrong type would surface as
# KeyError/TypeError deep inside a read, and an image key names a file. So the whole index is
# checked when it loads, and anything malformed is one BoardError.

_DT, _STR, _OPT = (_dt.datetime,), (str,), (str, type(None))
_MREF_TYPES = {
    "document_id": _STR, "bank": _STR, "job": _STR, "agent_key": _STR, "agent_name": _STR,
    "harness": _OPT, "host": _OPT, "session_id": _OPT, "tool_call_id": _OPT, "writer": _STR,
    "raw_bytes": (int,), "redactions": (int,), "stored_bytes": (int,), "patched": (bool,),
    "created_at": _DT, "checked_at": (_dt.datetime, type(None)), "images": (list, tuple),
}
_IMAGE_TYPES = {"sha256": _STR, "mime": _STR, "size": (int,), "first_seen": _DT}


def _check_memory_refs(refs) -> None:
    def bad(why: str):
        return BoardError(f"file board: {_TRANSCRIPTS}/{_MEMORY_REFS} is malformed ({why}): refusing to use it")

    def typed(v, types) -> bool:   # bool is an int in Python: only where bool is asked for
        return isinstance(v, types) and (bool in types or not isinstance(v, bool))

    if not isinstance(refs, dict):
        raise bad("not an object")
    for doc, row in refs.items():
        if not isinstance(row, dict):
            raise bad(f"row {doc!r} is not an object")
        for key, types in _MREF_TYPES.items():
            if key not in row or not typed(row[key], types):
                raise bad(f"row {doc!r}: {key} missing or of the wrong type")
        if row["document_id"] != doc:
            raise bad(f"row {doc!r} holds document_id {row['document_id']!r}")
        for img in row["images"]:
            if not isinstance(img, dict) or any(k not in img or not typed(img[k], t) for k, t in _IMAGE_TYPES.items()):
                raise bad(f"row {doc!r}: an image is missing a field or has one of the wrong type")
            if SHA256_RULE.fullmatch(img["sha256"]) is None:
                raise bad(f"row {doc!r}: image key {img['sha256']!r} is not a sha256")


# ---- the store ---------------------------------------------------------------------------

class _Transaction:
    """`store.lock`: reentrant; the outermost enter locks and loads, the outermost exit
    persists (unless the block raised) and unlocks."""

    def __init__(self, store: "FileStore"):
        self.store = store

    def __enter__(self):
        self.store._begin()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.store._end(commit=exc_type is None)
        return False


class FileStore(MemoryStore):
    """A MemoryStore whose rows live in a board directory. Use its attributes only inside
    `with store.lock:` (outside, they are whatever the last transaction loaded)."""

    def __init__(self, path: Path, read_only: bool = False):
        self._messages: list | None = []
        self._transcripts: dict | None = {}
        self._transcripts_text: str | None = None
        self._memory_refs: dict | None = {}
        self._memory_refs_text: str | None = None
        self._depth = 0
        super().__init__()
        self.path = Path(os.path.abspath(Path(path).expanduser()))
        self.read_only = read_only
        self.lock = _Transaction(self)       # replaces MemoryStore's RLock
        self._thread_lock = threading.RLock()
        self._closed = False
        self._lock_fd: int | None = None
        self._dir_fd: int | None = None
        # The directory, once, through safefs (no symlinked component, this user's, not writable
        # by others); every file below is opened relative to this descriptor (see _read).
        try:
            self._dir_fd = safefs.open_base(self.path, create=not read_only)
        except FileNotFoundError:
            raise FileNotFoundError(f"no file board at {self.path}") from None
        except ValueError as exc:
            raise OSError(str(exc)) from exc
        try:
            if read_only:
                try:   # shared flock on the existing lock file; never created
                    self._lock_fd = safefs.open_existing(self._dir_fd, _LOCK, os.O_RDONLY)
                except FileNotFoundError:
                    pass
            else:
                # created 0600 if missing, never truncated; fails (-> BoardUnavailable) where the
                # directory or the file is read-only, and for a planted link or FIFO
                self._lock_fd = safefs.open_lock(self._dir_fd, _LOCK)
        except BaseException:
            self.close()
            raise
        self._ro_seen: tuple = (None, None, None, None)   # read-only: (state, messages, transcripts,
                                                          # memory refs) as loaded
        self._state_text: str | None = None
        self._message_lines: list[str] = []
        self.lock_wait: float | None = None   # seconds _begin waits for the flock (None: no limit)

    def close(self) -> None:
        self._closed = True
        for attr in ("_lock_fd", "_dir_fd"):
            fd = getattr(self, attr, None)
            setattr(self, attr, None)
            if fd is not None:
                os.close(fd)

    def _refuse(self, what: str) -> None:
        if self.read_only:
            raise ReadOnlyBoard(f"file board {self.path} is open read-only: {what} refused")

    __del__ = close

    def touch(self, messages: bool = False) -> None:
        # No Condition to notify: watchers in other processes poll the files instead.
        if messages:
            self.msg_version += 1
        self.state_version += 1

    # messages load lazily: hooks like tool_started never read them
    @property
    def messages(self) -> list:
        if self._messages is None:
            self._load_messages()
        return self._messages

    @messages.setter
    def messages(self, value: list) -> None:
        if self._messages is None and self.in_transaction:
            self._load_messages()   # _save diffs against what is on disk
        self._messages = value

    # transcripts load lazily too: only the transcript methods read them
    @property
    def transcripts(self) -> dict:
        if self._transcripts is None:
            self._load_transcripts()
        return self._transcripts

    @transcripts.setter
    def transcripts(self, value: dict) -> None:
        if self._transcripts is None and self.in_transaction:
            self._load_transcripts()
        self._transcripts = value

    # memory refs load lazily as well: only the memory-ref and image methods read them
    @property
    def memory_refs(self) -> dict:
        if self._memory_refs is None:
            self._load_memory_refs()
        return self._memory_refs

    @memory_refs.setter
    def memory_refs(self, value: dict) -> None:
        if self._memory_refs is None and self.in_transaction:
            self._load_memory_refs()
        self._memory_refs = value

    @staticmethod
    def _body_name(key: str) -> str:
        return hashlib.sha1(key.encode("utf-8")).hexdigest() + ".xz"

    @contextlib.contextmanager
    def _in(self, *subdirs: str, create: bool = False):
        """The descriptor of transcripts/<subdirs...> for the block (None if missing)."""
        fds: list[int] = []
        try:
            fd = self._dir_fd
            for name in (_TRANSCRIPTS, *subdirs):
                fd = _sub(fd, name, create=create and not self.read_only)
                if fd is None:
                    break
                fds.append(fd)
            yield fd
        finally:
            for fd in fds:
                os.close(fd)

    def transcript_body(self, key: str) -> bytes | None:
        with self._in() as t:
            return None if t is None else _read(t, self._body_name(key), strict=False)

    def put_transcript_body(self, key: str, body: bytes) -> None:
        self._refuse("transcript body write")
        with self._in(create=True) as t:
            _write(t, self._body_name(key), body)

    def drop_transcript_body(self, key: str) -> None:
        self._refuse("transcript body removal")
        with self._in() as t:
            if t is not None:
                safefs.unlink(t, self._body_name(key))

    @staticmethod
    def _image_name(sha: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise ValueError(f"bad image sha256 {sha!r}")
        return sha

    def image_body(self, sha: str) -> bytes | None:
        name = self._image_name(sha)
        with self._in(_IMAGES) as t:
            return None if t is None else _read(t, name, strict=False)

    def put_image_body(self, sha: str, data: bytes) -> None:
        self._refuse("image write")
        name = self._image_name(sha)
        with self._in(_IMAGES, create=True) as t:
            _write(t, name, data)

    def drop_image_body(self, sha: str) -> None:
        self._refuse("image removal")
        name = self._image_name(sha)
        with self._in(_IMAGES) as t:
            if t is not None:
                safefs.unlink(t, name)

    def _load_transcripts(self) -> None:
        with self._in() as t:
            raw = None if t is None else _read(t, _TRANSCRIPT_INDEX, what=f"{_TRANSCRIPTS}/{_TRANSCRIPT_INDEX}")
        text = None if raw is None else raw.decode("utf-8")
        self._transcripts_text = text
        self._transcripts = loads(text) if text else {}
        if self.read_only:
            self._ro_seen = (*self._ro_seen[:2], dumps(self._transcripts), self._ro_seen[3])

    def _load_memory_refs(self) -> None:
        with self._in() as t:
            raw = None if t is None else _read(t, _MEMORY_REFS, what=f"{_TRANSCRIPTS}/{_MEMORY_REFS}")
        try:
            text = None if raw is None else raw.decode("utf-8")
            refs = loads(text) if text else {}
        except ValueError as exc:   # bad UTF-8 or JSON
            raise BoardError(f"file board: {_TRANSCRIPTS}/{_MEMORY_REFS} is not valid JSON: {exc}") from None
        _check_memory_refs(refs)
        self._memory_refs_text = text
        self._memory_refs = refs
        if self.read_only:
            self._ro_seen = (*self._ro_seen[:3], dumps(self._memory_refs))

    # next_id is only trustworthy once the messages are loaded (see _load_messages)
    @property
    def next_id(self) -> int:
        if self._messages is None and self.in_transaction:
            self._load_messages()
        return self._next_id

    @next_id.setter
    def next_id(self, value: int) -> None:
        self._next_id = value

    # ---- transactions

    def _begin(self) -> None:
        self._thread_lock.acquire()
        try:
            if self._depth == 0:
                if self._closed:
                    raise BoardError("board is closed")
                self._take_flock()
                try:
                    self._load_state()
                except BaseException:
                    self._unlock()
                    raise
            self._depth += 1
        except BaseException:
            self._thread_lock.release()
            raise

    def _unlock(self) -> None:
        if self._lock_fd is not None:
            compat.flock(self._lock_fd, compat.LOCK_UN)

    def _take_flock(self) -> None:
        if self._lock_fd is None:   # read-only without a lock file: nothing to lock
            return
        mode = compat.LOCK_SH if self.read_only else compat.LOCK_EX
        if self.lock_wait is None:
            compat.flock(self._lock_fd, mode)
            return
        deadline = time.monotonic() + self.lock_wait
        while True:
            try:
                compat.flock(self._lock_fd, mode | compat.LOCK_NB)
                return
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    msg = f"file board {self.path} stayed locked for {self.lock_wait:g}s"
                    raise BoardUnavailable(msg) from TimeoutError(msg)
                time.sleep(0.01)

    def _end(self, commit: bool) -> None:
        try:
            self._depth -= 1
            if self._depth == 0:
                try:
                    if commit:
                        self._check_unchanged() if self.read_only else self._save()
                finally:
                    self._messages = None   # never trust rows outside a transaction
                    self._transcripts = None
                    self._memory_refs = None
                    self._unlock()
        finally:
            self._thread_lock.release()

    @property
    def in_transaction(self) -> bool:
        return self._depth > 0

    # ---- load

    def _load_state(self) -> None:
        raw = _read(self._dir_fd, _STATE)
        text = None if raw is None else raw.decode("utf-8")
        state = loads(text) if text else {}
        if state.get("format", FORMAT) > FORMAT:
            raise BoardError(f"{self.path / _STATE} has format {state['format']}, newer than this "
                             f"swarm ({FORMAT}); upgrade the skill")
        self._state_text = text
        self.pool = state.get("pool") or {s: [] for s in NAME_SOURCES}
        self.jobs = state.get("jobs") or {}
        self.agents = state.get("agents") or {}
        self.routes = state.get("routes") or {}
        self.restarts = state.get("restarts") or []
        self.next_restart_id = int(state.get("next_restart_id") or 1)
        self.pauses = state.get("pauses") or []
        self.next_pause_id = int(state.get("next_pause_id") or 1)
        self.blockers = state.get("blockers") or []
        self.blocker_events = state.get("blocker_events") or []
        self.next_blocker_id = int(state.get("next_blocker_id") or 1)
        self.next_blocker_event_id = int(state.get("next_blocker_event_id") or 1)
        self.events = state.get("events") or []
        self.next_event_id = int(state.get("next_event_id") or 1)
        self.bg_commands = state.get("bg_commands") or []   # schema 24
        self.next_bg_id = int(state.get("next_bg_id") or 1)
        self._next_id = state.get("next_id", 1)
        self.message_max_chars = state.get("message_max_chars")
        self._messages = None
        self._transcripts = None
        self._memory_refs = None
        self._message_lines = []
        if self.read_only:
            self._ro_seen = (self._state_doc(with_next_id=False), None, None, None)

    def _load_messages(self) -> None:
        """Read messages.jsonl; under the lock, cut a torn last line (a crashed append).
        Read-only: skip it instead."""
        d = self._dir_fd
        raw = _read(d, _MESSAGES) or b""
        end = raw.rfind(b"\n") + 1
        if end != len(raw) and self.in_transaction and not self.read_only:
            try:   # the same checks as the read: never through a planted link
                fd = safefs.open_existing(d, _MESSAGES, os.O_WRONLY)
            except OSError as exc:
                raise _refused(_MESSAGES) from exc
            try:
                os.ftruncate(fd, end)
                os.fsync(fd)
            finally:
                os.close(fd)
        # split on "\n" only: str.splitlines also splits on U+2028, U+0085, \x1c... which
        # json.dumps(ensure_ascii=False) leaves raw inside a string (a job name, say)
        lines = raw[:end].decode("utf-8").split("\n")[:-1]
        self._message_lines = lines
        self._messages = [loads(line) for line in lines]
        if self._messages:
            self._next_id = max(self._next_id, self._messages[-1]["id"] + 1)
        if self.read_only:
            self._ro_seen = (self._ro_seen[0], [dumps(m) for m in self._messages], *self._ro_seen[2:])

    # ---- save

    def _state_doc(self, with_next_id: bool = True) -> str:
        doc = {"format": FORMAT, "next_id": self._next_id, "pool": self.pool,
               "jobs": self.jobs, "agents": self.agents, "routes": self.routes,
               "restarts": self.restarts, "next_restart_id": self.next_restart_id,
               "pauses": self.pauses, "next_pause_id": self.next_pause_id,
               "blockers": self.blockers, "blocker_events": self.blocker_events,
               "next_blocker_id": self.next_blocker_id, "next_blocker_event_id": self.next_blocker_event_id,
               "events": self.events, "next_event_id": self.next_event_id,
               "bg_commands": self.bg_commands, "next_bg_id": self.next_bg_id}
        if self.message_max_chars is not None:   # schema 15: the board's message cap
            doc["message_max_chars"] = self.message_max_chars
        if not with_next_id:   # loading the messages may raise it: not a change
            del doc["next_id"]
        return dumps(doc)

    def _check_unchanged(self) -> None:
        """Read-only commit: write nothing; raise if the transaction changed any row."""
        state, lines, transcripts, memory_refs = self._ro_seen
        if self._state_doc(with_next_id=False) != state:
            self._refuse("state change")
        if self._messages is not None and [dumps(m) for m in self._messages] != lines:
            self._refuse("message change")
        if self._transcripts is not None and dumps(self._transcripts) != transcripts:
            self._refuse("transcript change")
        if self._memory_refs is not None and dumps(self._memory_refs) != memory_refs:
            self._refuse("memory ref change")

    def _save(self) -> None:
        # Messages first: once a message is on disk, next_id can never hand its id out again.
        if self._messages is not None:
            lines = [dumps(m) for m in self._messages]
            old = self._message_lines
            if lines != old:
                if len(lines) >= len(old) and lines[:len(old)] == old:
                    _append(self._dir_fd, _MESSAGES, "".join(l + "\n" for l in lines[len(old):]).encode())
                else:
                    _write(self._dir_fd, _MESSAGES, "".join(l + "\n" for l in lines).encode())
                self._message_lines = lines
        if self._transcripts is not None:
            text = dumps(self._transcripts)
            if text != (self._transcripts_text or dumps({})):
                with self._in(create=True) as t:
                    _write(t, _TRANSCRIPT_INDEX, text.encode("utf-8"))
                self._transcripts_text = text
        if self._memory_refs is not None:   # after the excerpt bodies (put_transcript_body wrote them)
            text = dumps(self._memory_refs)
            if text != (self._memory_refs_text or dumps({})):
                with self._in(create=True) as t:
                    _write(t, _MEMORY_REFS, text.encode("utf-8"))
                self._memory_refs_text = text
        text = self._state_doc()
        if text != self._state_text:
            _write(self._dir_fd, _STATE, text.encode("utf-8"))
            self._state_text = text

    # ---- change signal

    def signature(self, messages_only: bool) -> tuple:
        """A cheap fingerprint that changes whenever a transaction wrote something."""
        names = (_MESSAGES,) if messages_only else (_MESSAGES, _STATE)
        sig = []
        for n in names:
            try:   # lstat relative to the directory: a planted link is not followed
                st = compat.stat(n, dir_fd=self._dir_fd, follow_symlinks=False)
                # a just-created, still empty messages.jsonl (the first post creates it, then
                # appends: two steps another process can observe between) is no change yet
                sig.append(None if n == _MESSAGES and st.st_size == 0
                           else (st.st_ino, st.st_mtime_ns, st.st_size))
            except (FileNotFoundError, TypeError):
                sig.append(None)
        return tuple(sig)


class FileBoard(MemoryBoard):
    """The memory backend's semantics over a FileStore: one machine, many processes."""

    @contextlib.contextmanager
    def op_timeout(self, seconds: float):
        """A transaction started within the block waits at most `seconds` for the board lock."""
        store = self._store
        old, store.lock_wait = store.lock_wait, max(0.0, seconds)
        try:
            yield
        finally:
            store.lock_wait = old

    def __init__(self, cfg: dict, read_only: bool = False):
        self._read_only = read_only
        super().__init__(cfg)
        if read_only:
            refuse_writes(self, f"file board {board_dir(cfg)}")

    def _make_store(self, cfg: dict) -> FileStore:
        return self._open_store(cfg, read_only=self._read_only)

    def _events_sleep(self, seconds: float) -> None:
        Board._events_sleep(self, seconds)   # other processes post: poll, there is no condition to wait on

    @classmethod
    def _open_store(cls, cfg: dict, read_only: bool = False) -> FileStore:
        path = board_dir(cfg)
        if str(path) in _UNAVAILABLE:
            msg = f"file board {path} is marked unavailable"
            raise BoardUnavailable(msg) from ConnectionError(msg)
        try:
            return FileStore(path, read_only=read_only)
        except OSError as exc:
            raise BoardUnavailable(str(exc)) from exc

    @classmethod
    def setup(cls, cfg, names) -> SetupResult:
        """MemoryBoard's (it opens a store through _open_store, creating the directory, and adds
        the names in one transaction), then records the schema version (never lowering it)."""
        result = super().setup(cfg, names)
        try:
            have = cls.schema_version(cfg) or 0
        except BoardUnavailable:   # a planted or broken version file: replaced below
            have = 0
        if have < SCHEMA_VERSION:
            with _open_dir(board_dir(cfg), create=True) as d:
                _write(d, _VERSION, f"{SCHEMA_VERSION}\n".encode())
        return result

    @classmethod
    def schema_version(cls, cfg: dict) -> int | None:
        path = board_dir(cfg)
        if str(path) in _UNAVAILABLE:
            msg = f"file board {path} is marked unavailable"
            raise BoardUnavailable(msg) from ConnectionError(msg)
        try:
            with _open_dir(path, create=False) as d:
                data = _read(d, _VERSION)
                if data is None:
                    # a board from before versions were recorded has a state.json
                    return 0 if safefs.exists(d, _STATE) else None
                return int(data.decode("utf-8", "replace").strip() or 0)
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise BoardUnavailable(str(exc)) from exc

    @classmethod
    def store_missing(cls, cfg: dict, cheap: bool = True) -> bool:
        # opening a file board creates its directory: the version file is what setup leaves
        return not (board_dir(cfg) / _VERSION).exists()

    @classmethod
    def identity(cls, cfg: dict) -> str:
        return str(board_dir(cfg).resolve())

    def close(self) -> None:
        super().close()
        self._store.close()

    def subscribe(self, messages_only: bool = False) -> None:
        self._s()
        self._subscribed = messages_only
        self._seen = self._store.signature(messages_only)

    def wait_for_change(self, timeout: float) -> bool:
        s = self._s()
        if self._subscribed is None:
            self.subscribe(False)
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            sig = s.signature(bool(self._subscribed))
            if sig != self._seen:
                self._seen = sig
                return True
            left = deadline - time.monotonic()
            if left <= 0:
                return False
            time.sleep(min(POLL_SECONDS, left))
