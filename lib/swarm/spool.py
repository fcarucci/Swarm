"""On-disk spool for posts (and memories) made while the board is unreachable.

Sandboxed agents often cannot open a connection to the board's backend; `swarm post` then queues
the message here with spool_post, and whichever process next opens a board (a hook, or the CLI)
delivers the queue with flush_spool. The spool directory must be writable from the agents' sandbox
and private to this user (0700; queue files 0600): one owned by another user is refused.

Three record types, one file each: posts are `<uuid>.json` (the original format), memories for
Hindsight (`swarm remember`) are `<uuid>.mem` and judges' verdicts (`swarm verdict`) are
`<uuid>.vrd`, so a flusher that predates them never touches them. A Hindsight outage never
blocks posts: memories wait, posts go through. A memory that has kept failing for 24 hours is
parked as `<uuid>.stuck` until `swarm spool retry` requeues it (see flush_spool).

Deliberately light on imports: the hooks import this on every tool call; the Hindsight client
is imported only when a memory is actually waiting.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import stat
import time
import uuid
from pathlib import Path
from swarm import compat, roles

STUCK_AFTER = 24 * 3600  # seconds a memory may keep failing before it is parked as .stuck
RECORD_MAX = 256 * 1024  # a spooled record larger than this is not read (it goes to .bad)
SUFFIXES = (".json", ".mem", ".vrd", ".wat")
WAIT_MAX = 500   # characters of a queued `wait --on` text


class SpoolError(RuntimeError):
    """The spool directory is not private to this user (owned by someone else, reached through
    a symlink or another user's directory, not a directory, or not creatable), so nothing is
    queued in it or delivered from it."""


# --------------------------------------------------------------------------- the directory
#
# The spool is written by sandboxed agents and read, renamed and deleted by unsandboxed hooks
# (a sandbox can plant anything there). So the directory is reached through verified descriptors
# (swarm.safefs: no symlink on the way, every component of this user's, below home or below
# root's sticky /tmp) and every file is handled relative to that descriptor, never by path:
# a symlinked spool dir, or one under another user's directory, is refused; a record that is a
# symlink, hard link, FIFO or another user's file is never read, followed or waited on.

def _dir_path(cfg: dict) -> Path:
    return Path(cfg["board"]["spool_dir"]).expanduser()


def _open_dir(cfg: dict, create: bool) -> int | None:
    """A verified descriptor of the spool directory (the caller closes it), or None if it is
    missing and not `create`. The directory must be this user's; a group- or world-accessible
    one of ours is made 0700 (the records in it are checked one by one anyway). Everything
    else, including any OSError while setting it up, is a SpoolError."""
    from swarm import safefs
    d = _dir_path(cfg)
    try:
        parent = safefs.open_base(d.parent, create=create)
    except FileNotFoundError:
        if create:
            raise SpoolError(f"spool directory {d}: its parent can't be created") from None
        return None
    except (OSError, ValueError) as exc:
        raise SpoolError(f"spool directory {d} refused: {exc}; set [board] spool_dir to a "
                         f"private directory") from exc
    try:
        if create:
            try:
                compat.mkdir(d.name, 0o700, dir_fd=parent)
            except FileExistsError:
                pass
        try:
            fd = compat.open(d.name, safefs.DIR_FLAGS, dir_fd=parent)
        except FileNotFoundError:
            if create:
                raise
            return None
        try:
            st = os.fstat(fd)
            if st.st_uid != compat.uid():
                raise SpoolError(f"spool directory {d} belongs to another user (uid {st.st_uid}); "
                                 f"set [board] spool_dir to a private directory")
            if stat.S_IMODE(st.st_mode) & 0o077:
                compat.fchmod(fd, 0o700)
        except BaseException:
            os.close(fd)
            raise
        return fd
    except SpoolError:
        raise
    except OSError as exc:   # a symlink or non-directory (ELOOP, ENOTDIR), EACCES...
        raise SpoolError(f"spool directory {d} refused: {exc}; set [board] spool_dir to a "
                         f"private directory") from exc
    finally:
        os.close(parent)


@contextlib.contextmanager
def _spool_dir(cfg: dict, create: bool):
    fd = _open_dir(cfg, create)
    try:
        yield fd
    finally:
        if fd is not None:
            os.close(fd)


def _mv(d: int, src: str, dst: str) -> None:
    compat.rename(src, dst, src_dir_fd=d, dst_dir_fd=d)


def _stem(name: str) -> str:
    return name.rsplit(".", 1)[0]


def _suffix(name: str) -> str:
    return "." + name.rsplit(".", 1)[1] if "." in name else ""


def _with_suffix(name: str, suffix: str) -> str:
    return _stem(name) + suffix


def _read_json(d: int, name: str) -> dict:
    """The record `name` (a private regular file with one link, at most RECORD_MAX bytes) as a
    dict; ValueError for anything else."""
    from swarm import safefs
    data = safefs.read(d, name, RECORD_MAX)
    if data is None:
        raise ValueError(f"{name} is not a private regular file of at most {RECORD_MAX} bytes")
    m = json.loads(data)
    if not isinstance(m, dict):
        raise ValueError("not a JSON object")
    return m


# --------------------------------------------------------------------------- queueing

def _spool(cfg: dict, record: dict, suffix: str) -> Path:
    from swarm import safefs
    with _spool_dir(cfg, create=True) as d:
        tmp = f".{uuid.uuid4().hex}.tmp"
        try:
            fd = safefs.create(d, tmp, 0o600)
        except OSError as exc:
            raise SpoolError(f"spool directory {_dir_path(cfg)}: can't write in it ({exc})") from exc
        try:
            compat.fchmod(fd, 0o600)
            safefs._write_all(fd, json.dumps({**record, "ts": time.time()}).encode())
        except BaseException:
            os.close(fd)
            with contextlib.suppress(OSError):
                compat.unlink(tmp, dir_fd=d)
            raise
        os.close(fd)
        final = tmp[1:].replace(".tmp", suffix)
        _mv(d, tmp, final)  # atomic: the flusher never sees a half-written file
    return _dir_path(cfg) / final


def spool_post(cfg: dict, job: str, name: str, message: str, to: str | None,
               agent_key: str | None = None) -> Path:
    """Queue a post on disk for the hooks to deliver (used when the board is unreachable).
    With `agent_key` (the posting agent, when the caller knows it) the post is delivered only if
    `name` is that agent's allocated name."""
    record = {"job": job, "name": name, "message": message, "to": to}
    if agent_key is not None:
        record["agent_key"] = agent_key
    return _spool(cfg, record, ".json")


def spool_memory(cfg: dict, job: str, name: str, text: str, project: str | None,
                 metadata: dict[str, str] | None = None, create_bank: bool = False) -> Path:
    """Queue a `swarm remember` for the hooks to deliver to Hindsight, with the provenance
    metadata the CLI knew (provenance.cli_metadata); it is stored as `swarm-spool-<stem>`."""
    return _spool(cfg, {"job": job, "name": name, "text": text, "project": project,
                        "metadata": dict(metadata or {}), "create_bank": create_bank}, ".mem")


def _valid_metadata(m) -> bool:
    """A queued memory's metadata (sandbox-writable data): provenance.valid_metadata, the rule
    cli_metadata already filters by, so the CLI's own record is never refused."""
    from swarm.provenance import valid_metadata   # only when a memory is waiting
    return valid_metadata(m)


def spool_verdict(cfg: dict, job: str, name: str, verdict: str, reason: str,
                  next_steps: str | None = None, artifact: str | None = None) -> Path:
    """Queue a `swarm verdict`; whether `name` is the job's judge is checked on delivery."""
    return _spool(cfg, {"job": job, "name": name, "verdict": verdict, "reason": reason,
                        "next": next_steps, "artifact": artifact}, ".vrd")


def spool_wait(cfg: dict, job: str, on: str | None, until: float | None = None) -> Path:
    """Queue a `swarm wait --on <on>` (on None: `swarm resume`) for the hooks to apply (a Codex sandbox has no network, so a Postgres board is out of its reach). `until`: epoch seconds when a bounded wait (`--for`) expires."""
    return _spool(cfg, {"job": job, "on": on, **({"until": until} if until else {})}, ".wat")


def pending_since(cfg: dict, job: str, since: float) -> bool:
    """Whether a post or wait for `job` has been queued here at or after `since` (epoch
    seconds) and not delivered yet: the auto-close sweep counts it as activity (a sandboxed
    agent's post that the hooks haven't delivered yet). Read-only; False on any trouble."""
    try:
        with _spool_dir(cfg, create=False) as d:
            if d is None:
                return False
            for name in _oldest_first(d, (".json", ".wat")):
                try:
                    if compat.stat(name, dir_fd=d, follow_symlinks=False).st_mtime < since:
                        continue
                    if _read_json(d, name).get("job") == job:
                        return True
                except (OSError, ValueError):
                    continue
    except (SpoolError, OSError):
        return False
    return False


# --------------------------------------------------------------------------- names

_NAME = re.compile(r"[A-Za-z0-9._'-](?:[A-Za-z0-9 ._'-]{0,62}[A-Za-z0-9._'-])?")


def valid_name(s) -> bool:
    """An agent name a spooled record may carry (board.base.valid_name where the board has
    it): 1-64 ASCII letters, digits, space and .'-_, no leading or trailing space."""
    try:
        from swarm.board.base import valid_name as board_valid_name
    except ImportError:
        return isinstance(s, str) and _NAME.fullmatch(s) is not None
    return board_valid_name(s)


def _valid_job(job) -> bool:
    from swarm.textsafe import has_controls
    return isinstance(job, str) and 0 < len(job) <= 256 and job == job.strip() and not has_controls(job)


# --------------------------------------------------------------------------- delivery

def _oldest_first(d: int, suffixes=SUFFIXES) -> list[str]:
    """The queued records, oldest first: regular files of this user with one link only (a
    symlink, hard link, FIFO or another user's file is left alone, never claimed)."""
    dated = []
    uid = compat.uid()
    for name in compat.listdir(d):
        if not name.endswith(suffixes) or name.startswith("."):
            continue
        try:
            st = compat.stat(name, dir_fd=d, follow_symlinks=False)
        except FileNotFoundError:
            continue  # claimed by a parallel flusher between the listing and the stat
        if not stat.S_ISREG(st.st_mode) or st.st_uid != uid or st.st_nlink != 1:
            continue
        dated.append((st.st_mtime, name))
    return [n for _, n in sorted(dated)]


def _claim(d: int, name: str) -> str | None:
    """Claim a spooled post by renaming it; None if another process claimed it first.

    Parallel hooks flush the same spool, and read-post-unlink without a claim would let two of
    them post the same message."""
    claimed = f"{_stem(name)}.sending-{os.getpid()}"
    try:
        compat.claim_rename(name, claimed, dir_fd=d)
    except (FileNotFoundError, FileExistsError):   # FileExistsError: Windows, another claimer holds the token
        return None
    return claimed


def _load(d: int, claimed: str, name: str) -> tuple | None:
    """(job, name, text, to-or-project, agent_key-or-metadata) of a claimed record (.wat:
    (job, on); .vrd: (job, name, verdict, reason, next)); a malformed one (or
    one that isn't a private regular file, or carries a name that isn't a plain agent name) is
    renamed to .bad (kept for inspection, never retried) and None returned."""
    suffix = _suffix(name)
    try:
        m = _read_json(d, claimed)
        if suffix == ".wat":
            from swarm.textsafe import has_controls
            on = m["on"]
            if not _valid_job(m["job"]) or not (on is None or (
                    isinstance(on, str) and 0 < len(on) <= WAIT_MAX and on.strip() and not has_controls(on))):
                raise ValueError("not a plain job name or wait text")
            until = m.get("until")   # absent: an unbounded wait (and in files of older versions)
            if until is not None and not (isinstance(until, (int, float)) and not isinstance(until, bool)
                                          and 0 < until < 4e9):
                raise ValueError("until is not a time")
            return m["job"], on, until
        if not _valid_job(m["job"]) or not valid_name(m["name"]):
            raise ValueError("not a plain job or agent name")
        if suffix == ".vrd":
            rec = m["job"], m["name"], m["verdict"], m["reason"], m.get("next"), m.get("artifact")   # optional in older files
            if rec[4] is not None and not isinstance(rec[4], str):
                raise ValueError("next is not text")
            if rec[5] is not None and (not isinstance(rec[5], str) or not rec[5].strip()):
                raise ValueError("artifact is not a nonempty reference")
            if rec[2] not in ("met", "not_met"):
                raise ValueError("unknown verdict")
            return rec
        is_memory = suffix == ".mem"
        rec = m["job"], m["name"], m["text" if is_memory else "message"], m.get("project" if is_memory else "to")
        if not rec[2].split():
            raise ValueError("empty message")  # would be refused forever and block the queue
        if is_memory:
            meta = m.get("metadata", {})   # absent (an older record) or a dict, nothing else
            if not _valid_metadata(meta):
                raise ValueError("metadata is not a small dict of plain strings")
            create_bank = m.get("create_bank", False)
            if not isinstance(create_bank, bool):
                raise ValueError("create_bank is not a boolean")
            rec = (*rec, meta, create_bank)
        else:
            if rec[3] is not None and not (valid_name(rec[3]) or
                    (isinstance(rec[3], str) and rec[3].startswith("@") and
                     roles.valid_name(rec[3][1:].lower()))):
                raise ValueError("not an agent name or role address")
            key = m.get("agent_key")
            if key is not None and not isinstance(key, str):
                raise ValueError("bad agent_key")
            rec = (*rec, key)
    except Exception:
        with contextlib.suppress(OSError):
            _mv(d, claimed, _with_suffix(name, ".bad"))
        return None
    return rec


def _backing_off(d: int, name: str, cfg: dict) -> bool:
    """A memory that failed less than retry_after_seconds ago waits (read without claiming it,
    so waiting costs no renames)."""
    try:
        last = _read_json(d, name).get("last_failed")
    except (OSError, ValueError, AttributeError):
        return False  # gone (claimed elsewhere) or malformed: let the claim and _load decide
    after = float((cfg.get("hindsight") or {}).get("retry_after_seconds", 60))
    return last is not None and time.time() - float(last) < after


def _rewrite(d: int, claimed: str, record: dict) -> None:
    """Replace a claimed record (a fresh file renamed over it: nothing is written through a
    link), keeping its mtime (its place in the queue)."""
    from swarm import safefs
    st = compat.stat(claimed, dir_fd=d, follow_symlinks=False)
    safefs.write_atomic(d, claimed, json.dumps(record))
    compat.utime(claimed, (st.st_atime, st.st_mtime), dir_fd=d, follow_symlinks=False)


def _record_failure(board, d: int, name: str, claimed: str, rec: tuple, bank: str, exc) -> str:
    """Note a failed attempt on the memory's file (attempts, first_failed, last_failed and
    last_error, with the server's detail) and put it back; after STUCK_AFTER seconds of
    failing, park it as `.stuck` and warn once on the job's board. Returns "kept", "stuck", or
    "board" if the warning couldn't be posted (it is kept and warned about next time)."""
    now = time.time()
    m = _read_json(d, claimed)
    m["attempts"] = int(m.get("attempts") or 0) + 1
    m.setdefault("first_failed", now)
    m["last_failed"] = now
    m["last_error"] = str(exc)
    outcome = "stuck" if now - float(m["first_failed"]) >= STUCK_AFTER else "kept"
    if outcome == "stuck":
        job, who = rec[0], rec[1]
        why = f"HTTP {exc.status}: {exc.detail}" if exc.status and exc.detail else str(exc)
        try:
            board.post(job, "swarm", f"memory from {who} for bank {bank} failed for 24h, parked as "
                                     f".stuck (requeue: swarm spool retry). {why}")
        except Exception:
            outcome = "board"
    _rewrite(d, claimed, m)
    _mv(d, claimed, _with_suffix(name, ".stuck") if outcome == "stuck" else name)
    return outcome


def _deliver_memory(board, cfg: dict, d: int, name: str, claimed: str, rec: tuple,
                    failed_banks: set) -> str:
    """Deliver one claimed memory. Returns "delivered"; "kept" (put back: it failed, or its bank
    already failed in this flush); "stuck"; "down" (Hindsight unreachable: put back, not
    counted as an attempt); or "board" (board trouble: put back)."""
    from swarm import hindsight  # only when a memory is waiting
    job, who, text, project, meta, create_bank = rec
    try:
        project = project or hindsight.project_of(board.job_status(job), job, cfg)
    except Exception:
        _mv(d, claimed, name)
        return "board"
    bank = hindsight.bank_id(project)
    if bank in failed_banks:
        _mv(d, claimed, name)
        return "kept"
    try:
        if not hindsight.enabled(cfg):
            raise hindsight.HindsightError("memory is off ([hindsight] url is empty)")
        # the record's own id: a retry after a reply that never came replaces, not duplicates
        hindsight.remember(board, cfg, job, who, text, project, document_id=f"swarm-spool-{_stem(name)}",
                           metadata=meta, create_bank=create_bank)
    except hindsight.HindsightUnavailable:
        _mv(d, claimed, name)
        return "down"
    except hindsight.HindsightError as exc:  # about this bank (5xx) or this item (4xx)
        if exc.bank_scoped:
            failed_banks.add(bank)
        return _record_failure(board, d, name, claimed, rec, bank, exc)
    except Exception:
        _mv(d, claimed, name)
        return "board"
    return "delivered"


def retry_stuck(cfg: dict) -> int:
    """Requeue every `.stuck` memory as `.mem` for the next flush, with a fresh 24 hours (its
    attempts and last_error are kept). Returns how many were requeued."""
    try:
        cm = _spool_dir(cfg, create=False)
        d = cm.__enter__()
    except SpoolError:
        return 0
    try:
        if d is None:
            return 0
        n = 0
        for name in sorted(_oldest_first(d, (".stuck",))):
            claimed = _claim(d, name)
            if not claimed:
                continue
            try:
                m = _read_json(d, claimed)
                for key in ("first_failed", "last_failed"):
                    m.pop(key, None)
                _rewrite(d, claimed, m)
            except (OSError, ValueError):
                _mv(d, claimed, name)  # unreadable: leave it parked
                continue
            _mv(d, claimed, _with_suffix(name, ".mem"))
            n += 1
        return n
    finally:
        cm.__exit__(None, None, None)


def deliver_verdict(board, job: str, name: str, verdict: str, reason: str,
                    next_steps: str | None = None, artifact: str | None = None) -> bool:
    """Record a judge's verdict and post it on the job's board; False (nothing recorded or
    posted) if `name` is not the job's active judge. Shared by `swarm verdict` and the spool."""
    if artifact is None:
        from swarm.review import latest_handoffs, judge_artifact, branch_revision
        leading, separator, _ = reason.partition(':')
        artifact = leading if separator and branch_revision(leading) else None
        artifact = artifact or judge_artifact(board, job, name=name)
        handoffs = latest_handoffs(board, job)
        if artifact is None and handoffs:
            artifact = handoffs[-1].artifact
    if not board.record_verdict(job, name, verdict, reason, next_steps, artifact):
        return False
    board.post(job, name, f"VERDICT {verdict}: {reason}")
    if next_steps:   # the board caps a message; the full text is what `swarm status --job J` shows
        board.post(job, name, f"NEXT (to meet the goal; full text in swarm status): {next_steps}")
    return True


def _deliver_spooled_verdict(board, rec: tuple) -> bool:
    """A spooled verdict from someone who isn't the judge is refused; the sender is told on the
    board (it can't see the CLI's answer: the CLI only queued it)."""
    job, name, verdict, reason, next_steps, artifact = rec
    if deliver_verdict(board, job, name, verdict, reason, next_steps, artifact):
        return True
    board.post(job, "swarm", f"verdict refused: {name} is not the judge of job {job}", to=name)
    return False


def _deliver_post(board, rec: tuple) -> bool:
    """Validate a queued author and resolve recipients against the roster at delivery time.
    A refused post tells its author why on the board before the record is parked."""
    job, name, message, to, key = rec
    from swarm import addressing
    try:
        addressing.check_author(board, job, name, key)
        targets = addressing.resolve(board, job, to) if to else [None]
    except addressing.AddressError as exc:
        board.post(job, "swarm", f"not delivered: {exc}", to=name)
        return False
    for target in targets:
        board.post(job, name, message, to=target, agent_key=key)
    return True


def _deliver(board, cfg: dict, d: int, name: str, claimed: str, rec: tuple, failed_banks: set) -> str:
    """Deliver one claimed record: "delivered" (its file is gone), "skip" (put back or parked;
    go on), "down" (Hindsight unreachable: skip memories for the rest of the flush) or "stop"
    (board trouble: put back; stop the flush)."""
    from swarm.board.base import JobPaused
    suffix = _suffix(name)
    if suffix == ".mem":
        outcome = _deliver_memory(board, cfg, d, name, claimed, rec, failed_banks)
        if outcome == "delivered":
            compat.unlink(claimed, dir_fd=d)
        if outcome in ("delivered", "down"):
            return outcome
        return "stop" if outcome == "board" else "skip"
    try:
        if suffix == ".wat":
            import datetime as dt
            accepted = bool(board.set_waiting(   # False: the job isn't open
                rec[0], rec[1], dt.datetime.fromtimestamp(rec[2], dt.timezone.utc) if rec[2] else None))
        elif suffix == ".vrd":
            accepted = _deliver_spooled_verdict(board, rec)
        else:
            accepted = _deliver_post(board, rec)
    except ValueError:   # the board will never take it (a name or text it refuses): not retried
        accepted = False
    except JobPaused:    # kept for after the resume; the rest of the spool goes on
        _mv(d, claimed, name)
        return "skip"
    except Exception:
        _mv(d, claimed, name)  # board trouble: put it back for the next flush
        return "stop"
    if not accepted:
        _mv(d, claimed, _with_suffix(name, ".bad"))
        return "skip"
    compat.unlink(claimed, dir_fd=d)
    return "delivered"


OP_FLOOR = 0.05   # seconds a board call within a delivery always gets (bookkeeping after a
                 # memory was stored must not fail for lack of time: it would be stored twice)


class _DeadlineBoard:
    """The board as one delivery sees it: every call runs under board.op_timeout(what is left
    of the delivery's deadline, at least OP_FLOOR), so several calls share one budget."""

    def __init__(self, board, deadline: float):
        self._board, self._deadline = board, deadline

    def __getattr__(self, name):
        attr = getattr(self._board, name)
        limit = getattr(self._board, "op_timeout", None)
        if not callable(attr) or limit is None:
            return attr

        def bounded(*a, **k):
            with limit(max(OP_FLOOR, self._deadline - time.monotonic())):
                return attr(*a, **k)
        return bounded


def _bounded(board, cfg: dict, seconds: float | None):
    """(board, cfg) for one delivery that may take at most `seconds` (None: no bound): every
    board call bounded by what is left (_DeadlineBoard), and Hindsight's calls by one absolute
    deadline for them all ([hindsight] deadline, see hindsight.Client)."""
    if seconds is None:
        return board, cfg
    deadline = time.monotonic() + seconds
    h = dict(cfg.get("hindsight") or {})
    h["timeout_seconds"] = min(float(h.get("timeout_seconds", seconds)), seconds)
    h["deadline"] = deadline
    return _DeadlineBoard(board, deadline), {**cfg, "hindsight": h}


def flush_spool(board, cfg: dict, max_items: int | None = None, deadline: float | None = None,
                op_timeout: float | None = None, memories: bool = True) -> int:
    """Deliver spooled posts through board.post, in the order they were written.

    Returns how many were delivered. Bounds, for the hooks (the CLI passes none): at most
    `max_items` records are attempted, none is started after `deadline` (time.monotonic()),
    and each delivery may take at most `op_timeout` seconds and no longer than what is left
    before `deadline`, all its calls together (see _bounded);
    the rest waits for a later flush. memories=False leaves every memory queued (the per-tool
    hooks: a Hindsight call may block where a board post doesn't). Nothing is read from a spool directory that is not
    private to this user (see _open_dir). Stops at the first post or verdict that fails (it
    is put back for the next flush); a verdict refused because its sender isn't the judge goes
    to .bad.

    Memories never hold up posts, and never hold up each other unless Hindsight as a whole is
    unreachable (then they all wait, uncounted). An error answer is scoped: a 5xx skips that
    bank's other memories for the rest of this flush, a 4xx only that memory. Either way the
    failed memory records attempts, first_failed, last_failed and last_error on its file, and
    waits retry_after_seconds before its next attempt; after STUCK_AFTER (24 hours) of failing
    it is renamed `.stuck` with one warning on the job's board. The flusher leaves `.stuck`
    alone; `swarm spool retry` (retry_stuck) requeues it. No memory goes to .bad unless its
    file is malformed, and nothing in .bad is retried.
    """
    try:
        d = _open_dir(cfg, create=False)
    except SpoolError:
        return 0
    if d is None:
        return 0
    try:
        return _flush(board, cfg, d, max_items, deadline, op_timeout, memories)
    finally:
        os.close(d)


def _flush(board, cfg: dict, d: int, max_items, deadline, op_timeout, memories: bool) -> int:
    delivered, attempted, memory_down, failed_banks = 0, 0, False, set()
    for name in _oldest_first(d):
        left = None if deadline is None else deadline - time.monotonic()
        if (max_items is not None and attempted >= max_items) or (left is not None and left <= 0):
            break
        if name.endswith(".mem") and (not memories or memory_down or _backing_off(d, name, cfg)):
            continue
        attempted += 1
        claimed = _claim(d, name)
        rec = claimed and _load(d, claimed, name)
        if not rec:
            continue
        seconds = min(x for x in (left, op_timeout) if x is not None) \
            if (left, op_timeout) != (None, None) else None
        bounded_board, bounded_cfg = _bounded(board, cfg, seconds)
        outcome = _deliver(bounded_board, bounded_cfg, d, name, claimed, rec, failed_banks)
        if outcome == "stop":
            return delivered
        if outcome == "down":
            memory_down = True
        elif outcome == "delivered":
            delivered += 1
    return delivered
