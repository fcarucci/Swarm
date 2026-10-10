"""Job markers and the host log, for the hooks and the CLI alike.

Neutral on purpose (stdlib and swarm.safefs/paths only): `swarm activate`, the plugin identity
and the hooks all read the marker dir, and the directory edition ships without swarm.hooks.
swarm.hooks re-exports every name here, so existing callers are unchanged."""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

MARKER_MAX_BYTES = 64 * 1024   # a marker is a few hundred bytes; anything bigger is not one
_SKIPPED_LOGGED: set = set()   # marker names already logged as refused by this process


JOB_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")   # always fullmatch


def _valid_job(job) -> bool:
    """A job name a marker may name: JOB_NAME, as `activate` requires (markers are
    sandbox-writable: the name ends up in the commands shown to agents)."""
    return isinstance(job, str) and JOB_NAME.fullmatch(job) is not None


def _markers(cfg: dict) -> list[dict]:
    """The job markers in the marker dir, read without following links or blocking (the dir is
    sandbox-writable: safefs.scan skips symlinks, hard links, FIFOs, other users' files and
    anything over MARKER_MAX_BYTES). A marker without a valid job name is ignored too."""
    from swarm import safefs
    d = Path(cfg["hook"]["marker_dir"]).expanduser()
    try:
        fd = safefs.open_base(d, create=False)
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        _log_line("markers", "-", f"marker dir {d} refused: {exc}")
        return []
    out, skipped = [], []
    try:
        entries = safefs.scan(fd, ".json", limit=MARKER_MAX_BYTES, skipped=skipped)
        mtimes = {name: safefs.mtime(fd, name) for name, _ in entries}
    except OSError as exc:
        _log_line("markers", "-", f"marker dir {d} unreadable: {exc}")
        return []
    finally:
        os.close(fd)
    for name in skipped:
        if name not in _SKIPPED_LOGGED:
            _SKIPPED_LOGGED.add(name)
            _log_line("markers", "-", f"skipped {name} in {d}: not a private regular file of this "
                                       f"user, or over {MARKER_MAX_BYTES} bytes")
    for name, data in entries:
        try:
            m = json.loads(data)
        except ValueError:
            continue
        if not isinstance(m, dict) or not _valid_job(m.get("job")):
            continue
        m["_path"], m["_mtime"] = d / name, mtimes.get(name)
        out.append(m)
    return out


def _append_host_log(name: str, line: str) -> None:
    """Append one line to `name` in the host-only dir (paths.host_dir(): 0700, outside every
    sandbox writable root), made one printable line first (safefs.log_safe: payload, marker and
    board data can hold newlines). Through safefs: a planted symlink, hard link or FIFO is
    refused, never followed or waited on. Never raises."""
    try:
        from swarm import paths, safefs
        with safefs.dir_fd(paths.host_dir(), strict_mode=0o700) as d:
            safefs.append(d, name, safefs.log_safe(line) + "\n")
    except Exception:
        pass


def _log_line(event: str, agent_id: str, text: str) -> None:
    """Append one line to hook-errors.log (in the host-only dir); never raises."""
    _append_host_log("hook-errors.log", f"{time.strftime('%F %T')} {event} {agent_id}: {text}")
