"""Resume markers: an ordinary job marker (<marker_dir>/<job>--resume-r<restart id>.json, so
deactivate and the auto-close marker cleanup remove it) with a "resume" section. The hooks
treat a root session bound to one as the replacement agent resume.agent_key. Claude
replacements are bound when written (claude -p --session-id); Codex ones once the runner reads
the thread id (bind_session).

Codex binding race. `thread.started` reaching the runner does not guarantee its bind_session
finished before the session's first hook runs. So the runner also gives each Codex replacement
a random token (set_resume_token, before the session starts) in the environment of `codex exec`
(TOKEN_ENV; Codex hooks run with a snapshot of that environment, codex-rs/hooks/src/registry.rs).
A hook whose session isn't bound yet finds its marker by that token and binds it itself
(resume_by_token): whichever of the runner and the hook comes first binds, the other agrees.

Locking. A marker's own lock (cli.locked_marker) can't be taken before the marker exists, so
creation is coordinated with removal through a second lock that always exists: lock_path(), one
file per marker directory (0600, never deleted, not *.json). write_resume_marker,
bind_session and remove_resume_marker all hold it: under it, "is the marker there?" and the
create (_create: complete when it appears, never over another file), rewrite
or removal can't interleave. Rewrites and removals also hold the marker's own lock, like the
hooks' claims. A removal that doesn't take the directory lock (deactivate, the auto-close
cleanup) is never undone: a marker found gone mid-write is not recreated. Files only: safe on
the per-tool hook path.

Planted files. The marker directory is sandbox-writable, so every
read here goes through safefs on a descriptor of the directory (opened without following a
symlink anywhere below $HOME): a FIFO never blocks, and a symlink, hard link, another user's file
or one over MARKER_MAX bytes is skipped (logged once per name), never read."""
from __future__ import annotations

import contextlib
import json
import os
import time
from pathlib import Path

RESUME = "resume"
TOKEN_ENV = "SWARM_RESUME_TOKEN"   # a Codex replacement's token (the runner sets it for codex exec)
LOCK_WAIT = 5.0   # seconds the directory lock and a marker's own lock are waited for
LOCK_NAME = ".resume-markers.lock"
MARKER_MAX = 64 * 1024   # a marker is a few hundred bytes: anything bigger is not one
_skipped_logged: set = set()


def lock_path(marker_dir: Path) -> Path:
    return marker_dir / LOCK_NAME


@contextlib.contextmanager
def _dir_lock(marker_dir: Path, timeout: float):
    """The marker directory's resume lock (exclusive flock), created 0600 if missing. Yields
    whether it was had within `timeout` seconds. Fails closed: the directory is opened through
    safefs (no symlink anywhere below $HOME, this user's), and a lock that is a symlink (OSError),
    not a regular file, a hard link or another user's file (PermissionError) is never used; a
    looser mode is tightened to 0600."""
    import fcntl
    from swarm import safefs
    d = safefs.open_base(marker_dir, create=True)
    try:
        fd = safefs.open_lock(d, LOCK_NAME)
    finally:
        os.close(d)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    yield False
                    return
                time.sleep(0.02)
        yield True
    finally:
        os.close(fd)   # releases the lock


def _log_skipped(d: Path, names: list) -> None:
    from swarm.safefs import log_safe
    from swarm.supervisor.settings import log
    for n in names:
        if (str(d), n) not in _skipped_logged:
            _skipped_logged.add((str(d), n))
            log(f"skipped resume marker {log_safe(n)} in {log_safe(d)}: not a private regular file "
                f"of at most {MARKER_MAX} bytes (a FIFO, link or planted file?)")


def _parse(data: bytes | None) -> dict | None:
    if data is None:
        return None
    try:
        m = json.loads(data)
    except ValueError:
        return None
    return m if isinstance(m, dict) else None


def _write(path: Path, text: str) -> None:
    """Replace the marker at `path` (0600) through a verified descriptor of its directory
    (safefs.write_atomic): a planted link or FIFO is replaced, never followed."""
    from swarm import safefs
    path = Path(path)
    with safefs.dir_fd(path.parent, create=False) as d:
        safefs.write_atomic(d, path.name, text, 0o600)


def _create(path: Path, text: str) -> None:
    """Create the marker at `path` (0600) complete or not at all, never over anything already
    there (FileExistsError): a temp file in the verified directory, hard-linked into place."""
    import secrets
    from swarm import safefs
    path = Path(path)
    with safefs.dir_fd(path.parent, create=True) as d:
        tmp = f".{path.name}.{secrets.token_hex(6)}.swarm-tmp"
        fd = safefs.create(d, tmp, 0o600)
        try:
            try:
                os.fchmod(fd, 0o600)
                os.write(fd, text.encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
            os.link(tmp, path.name, src_dir_fd=d, dst_dir_fd=d, follow_symlinks=False)
        finally:
            safefs.unlink(d, tmp)
        with contextlib.suppress(OSError):
            os.fsync(d)


def _planted(path: Path) -> bool:
    """Whether something other than a private regular file with one link (a FIFO, symlink, hard
    link, directory, another user's file) sits at `path`: never opened, never written through.
    A missing path is not planted."""
    import stat
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_nlink != 1


def read_marker(path: Path) -> dict | None:
    """A marker's JSON object, read safely (safefs: never through a link, never blocking on a
    FIFO, at most MARKER_MAX bytes); None if it is missing, refused or not a JSON object."""
    from swarm import safefs
    path = Path(path)
    try:
        d = safefs.open_base(path.parent, create=False)
    except (OSError, ValueError):
        return None
    try:
        return _parse(safefs.read(d, path.name, MARKER_MAX))
    except (OSError, ValueError):
        return None
    finally:
        os.close(d)


def _resume_markers(cfg: dict) -> list[tuple[Path, dict]]:
    """(path, JSON object) of every resume marker in the marker directory, by name, read through
    safefs.scan (see the module docstring); [] when the directory is missing or unsafe."""
    from swarm import safefs
    d = _dir(cfg)
    try:
        fd = safefs.open_base(d, create=False)
    except (OSError, ValueError):
        return []
    skipped: list = []
    try:
        found = safefs.scan(fd, ".json", limit=MARKER_MAX, match=lambda n: "--resume-r" in n,
                            skipped=skipped)
    except OSError:
        return []
    finally:
        os.close(fd)
    if skipped:
        _log_skipped(d, skipped)
    out = []
    for name, data in found:
        m = _parse(data)
        if m is not None:
            out.append((d / name, m))
    return out


def _dir(cfg: dict) -> Path:
    return Path(cfg["hook"]["marker_dir"]).expanduser()


def resume_marker_path(cfg: dict, job: str, restart_id: int) -> Path:
    from swarm.cli import safe_job
    return _dir(cfg) / f"{safe_job(job)}--resume-r{int(restart_id)}.json"


def write_resume_marker(cfg: dict, job: str, restart_id: int, *, resume_of: str, name: str,
                        harness: str, session_id: str | None = None) -> Path:
    """Write the marker (0600) and return its path, under the directory lock. A marker already
    at that path (the same restart written again) is rewritten under its own lock too.
    TimeoutError if a lock isn't had within LOCK_WAIT seconds; FileNotFoundError if the marker
    was there and got removed meanwhile (a removal is never undone)."""
    from swarm.cli import locked_marker
    path = resume_marker_path(cfg, job, restart_id)
    text = json.dumps({"job": job, "session_id": session_id, "adopt_running": False,
                       RESUME: {"agent_key": session_id, "resume_of": resume_of, "name": name,
                                "harness": harness, "restart_id": int(restart_id)}})
    with _dir_lock(path.parent, LOCK_WAIT) as had:
        if not had:
            raise TimeoutError(f"resume markers in {path.parent} are busy")
        try:
            _create(path, text)
            return path
        except FileExistsError:
            pass
        # raises FileNotFoundError if it went meanwhile (deactivate): not recreated
        with locked_marker(path, LOCK_WAIT) as fh:
            if fh is None:
                raise TimeoutError(f"resume marker {path.name} is busy")
            _write(path, text)
            return path


def bind_session(path: Path, session_id: str) -> bool:
    """Bind an unbound resume marker to session_id (agent_key too). False if it is gone, bound to
    another session, or its locks weren't had within a second each."""
    from swarm.cli import locked_marker
    try:
        with _dir_lock(path.parent, 1.0) as had:
            if not had or _planted(path):
                return False
            with locked_marker(path, 1.0) as fh:
                return fh is not None and _bind(fh, path, session_id, _write)
    except (FileNotFoundError, ValueError):
        return False


def _bind(fh, path: Path, session_id: str, write) -> bool:
    m = json.loads(fh.read() or "{}")
    if not isinstance(m, dict) or m.get("session_id") not in (None, session_id):
        return False
    m["session_id"] = session_id
    r = m[RESUME] if isinstance(m.get(RESUME), dict) else m.setdefault(RESUME, {})
    if not isinstance(r, dict):
        return False
    r["agent_key"] = r.get("agent_key") or session_id
    write(path, json.dumps(m))   # 0600, through safefs (_write)
    return True


def resume_binding(cfg: dict, session_id: str) -> dict | None:
    if not session_id:
        return None
    for p, m in _resume_markers(cfg):
        if m.get("session_id") == session_id and isinstance(m.get(RESUME), dict):
            m["_path"] = p
            return m
    return None


def remove_resume_marker(path: Path) -> bool:
    """Remove the marker under the directory lock and its own (cli.remove_marker). True if it is
    gone; False, marker left, if a lock wasn't had within LOCK_WAIT seconds."""
    from swarm.cli import remove_marker
    if not path.parent.is_dir():
        return True
    with _dir_lock(path.parent, LOCK_WAIT) as had:
        if had and _planted(path):   # a FIFO or link swapped in: unlinked, never opened
            from swarm import safefs
            try:
                with safefs.dir_fd(path.parent, create=False) as d:
                    safefs.unlink(d, path.name)
            except FileNotFoundError:
                pass
            except OSError:
                return False
            return True
        return had and remove_marker(path, wait=LOCK_WAIT)


def set_resume_token(path: Path, token: str) -> bool:
    """Store the replacement's token in its unbound-or-bound resume marker (the runner, before the
    session starts). False if the marker is gone or its locks weren't had within LOCK_WAIT."""
    from swarm.cli import locked_marker
    try:
        with _dir_lock(path.parent, LOCK_WAIT) as had:
            if not had or _planted(path):
                return False
            with locked_marker(path, LOCK_WAIT) as fh:
                return fh is not None and _set_token(fh, path, token, _write)
    except (FileNotFoundError, ValueError):
        return False


def _set_token(fh, path: Path, token: str, write) -> bool:
    m = json.loads(fh.read() or "{}")
    if not isinstance(m, dict) or not isinstance(m.get(RESUME), dict):
        return False
    m[RESUME]["token"] = token
    write(path, json.dumps(m))
    return True


def resume_by_token(cfg: dict, token: str, session_id: str) -> dict | None:
    """The resume marker carrying `token`, bound to session_id (binding it now if it is unbound:
    the hook got there before the runner). None if no marker has the token, or it is bound to
    another session. Files only."""
    import hmac
    if not token or not session_id:
        return None
    for p, m in _resume_markers(cfg):
        r = m.get(RESUME)
        if not isinstance(r, dict) or not isinstance(r.get("token"), str) \
                or not hmac.compare_digest(r["token"], token):
            continue
        if m.get("session_id") is None:
            bind_session(p, session_id)   # False when the runner bound it first: re-read below
        return resume_binding(cfg, session_id)
    return None
