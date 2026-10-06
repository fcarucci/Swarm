"""`swarm supervise`: one pass of the supervisor. The systemd user timer runs it
every [supervise] timer_minutes; `--dry-run` prints what it would do and changes nothing (no
board write, no file written: not even the run locks, the outage file or the state).

A pass: the kill switches; the supervisor's private directory (settings.ensure_private_dir: refused
if unsafe); the pass lock (supervise.lock in it, flock: one pass at
a time per machine and user, so the once-only posts and the host caps are decided by one pass);
the board (unreachable: the outage is noted and nothing is done); the runner's dead runs reaped,
leftover resume markers cleaned and the sweep (closes stuck agents); then each candidate (a
stuck-closed agent of this machine+user on an open, supervised job without a met verdict, not
replaced yet, whose name is free) is decided again right before acting, with the restarts and
launches of this pass, and revalidated right before its restart row is recorded. The job cap
holds across hosts: Board.record_restart counts it atomically. A cap is posted once per host
and job; waits (backoff, concurrency) are logged. Without a user systemd manager nothing is
restarted (the runner could only refuse): that is posted once per host and job, not recorded.
Then the caps summary.

Launch authority. Board rows are written by agents (sandboxed or not)
and, through the shared database role, by another OS user, so no board field decides a launch.
An agent is this host user's to replace only if the unsandboxed hook enrolled it here: a
host-private enrolment record (swarm.enrolment) for this board and agent key, of the row's job.
The replacement's work dir and harness come from that record only (never from the board's copy
of the transcript or from an orchestrator marker); a row whose harness disagrees is refused. The
work dir is still checked against [supervise] allowed_workdirs, and a dir holding project
configuration (workdir_config_problem: .claude/settings*.json, .claude/hooks, .claude/agents,
.mcp.json, .codex, AGENTS.override.md, in it or in a parent up to the allowed root) must have
been approved out of band (`swarm supervise approve`): each file's hash is recorded in the
private approval store (approved.json), and a changed file is unapproved again."""
from __future__ import annotations

import datetime as _dt
import getpass
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from swarm.board import AgentStatus, JobStatus, board_backend
from swarm.supervisor.budget import Decision
from swarm import compat

NO_MANAGER_NOTE = "not restarting: no user systemd manager (fix: loginctl enable-linger $USER)"
BUSY = "another supervise pass is running: nothing done"
_RESUME_ID = re.compile(r"--resume-r(\d+)\.json$")


@dataclass(frozen=True)
class Candidate:
    js: JobStatus
    agent: AgentStatus
    lineage: str
    decision: Decision
    outage: bool
    record: object = None   # its enrolment.Record: the only source of its work dir and harness


# The pass runs as a systemd oneshot with TimeoutStartSec=110 (supervisor.systemd): its
# transcript work is bounded to stay well inside that. The sweep (stuck closing, the
# sweeps' pending finals, auto-close captures) gets PASS_SWEEP_SECONDS, the capture before a
# restart's brief BRIEF_CAPTURE_SECONDS, and the retries of slow pending finals (60 s each,
# lost.retry_slow_finals) whatever is left of PASS_BUDGET_SECONDS, last.
PASS_BUDGET_SECONDS = 95.0
PASS_SWEEP_SECONDS = 20.0
BRIEF_CAPTURE_SECONDS = 10.0
NOT_ENROLLED = "no local enrolment record (not enrolled by this host user's hooks: never replaced)"


def enrolment_of(cfg: dict, a, job: str):
    """The host-private enrolment record that makes agent row `a` of `job` this host user's,
    or None: a missing, unsafe or invalid record, or one for another job."""
    from swarm import enrolment
    from swarm.board.autoinit import store_key
    try:
        rec = enrolment.find(store_key(cfg), a.agent_key)
    except (OSError, ValueError):
        return None
    return rec if rec is not None and rec.job == job else None


def _host() -> str:
    return compat.node()


def exposure_refusal() -> str | None:
    """The user's Codex config gives sandboxes a writable root
    covering (or inside) ~/.local/share/swarm, so the assumption privfs and the host records rest
    on is false. Why the pass must not run, or None."""
    from swarm import paths
    from swarm.supervisor.settings import codex_exposure
    roots = codex_exposure()
    if not roots:
        return None
    return (f"refusing to supervise: Codex writable_roots {', '.join(map(str, roots))} overlap "
            f"{paths.share_dir()}: sandboxed Codex agents could write the supervisor's private "
            f"directory and the host records (remove that root from the Codex config)")


def lock_path() -> Path:
    from swarm.supervisor.settings import private_dir
    return private_dir() / "supervise.lock"


def _take_pass_lock() -> int | None:
    """The pass lock (exclusive, non-blocking): its descriptor, or None when another pass has it."""
    from swarm.supervisor import privfs
    from swarm import safefs
    with privfs.dir_fd() as d:
        fd = safefs.open_wlock(d, lock_path().name)   # 0200, write-only: a reader can't hold it
    try:
        compat.flock(fd, compat.LOCK_EX | compat.LOCK_NB)
        return fd
    except OSError:
        os.close(fd)
        return None


def _decide_for(board, cfg: dict, sup: dict, js, a, rows, job_rs, host_rs, now, running: int,
                record=None) -> Candidate:
    from swarm.supervisor import budget, outage
    out = outage.current()
    dead = _dt.timedelta(minutes=float(cfg["board"]["dead_minutes"]))
    in_outage = bool(out and out.recovered and out.affects(a.last_contact_at, dead))
    lineage = budget.lineage_root(a.agent_key, {x.agent_key: x for x in rows})
    d = budget.decide(sup, lineage=lineage, job_restarts=job_rs, host_restarts=host_rs,
                      closed_at=a.ended_at, now=now, running=running,
                      outage_started=out.started if in_outage else None)
    return Candidate(js, a, lineage, d, in_outage, record)


def _host_restarts(board):
    """This host's restarts that count toward today's minutes: every OS user's (the caps are per
    host), every one that overlaps today (budget.day_rows)."""
    from swarm.supervisor import budget
    from swarm.supervisor.settings import today_start
    return budget.day_rows(board.restarts(host=_host()), today_start())


def _host_running(board) -> int:
    """Replacements running on this host, from the board: open restart rows of every OS user."""
    from swarm.supervisor import budget
    return budget.host_running(board.restarts(host=_host()))


def candidates(board, cfg: dict, sup: dict, *, job: str | None = None, now=None,
               running: int = 0, unenrolled: list | None = None) -> list[Candidate]:
    """This host user's stuck-closed agents (enrolled here: enrolment_of) that may be replaced,
    each with its budget decision (go, or why not). `running`: live replacements on this host;
    each go counts. `unenrolled`: if given, gets (job status, agent) of the stuck-closed rows that
    claim this host and OS user but have no enrolment record (forged, or from before 0.1.0).
    Read-only. The decisions are a preview: run_pass decides each again before acting."""
    from swarm.board import STUCK_PREFIX
    now = now or board.now()
    host_rs = _host_restarts(board)
    found = []
    for js in board.jobs(False):
        # The job still needs work and supervise is enabled.
        if (job and js.job != job) or not js.supervise or js.status != "active" or js.verdict == "met" or \
                (js.waiting_on and not js.waiting_on.startswith("supervisor: restarting ")):
            continue
        from swarm.supervisor.orphans import human_question
        if human_question(board, js):
            continue
        rows = board.agents(js.job)
        job_rs = board.restarts(job=js.job)
        replaced = {r.old_agent_key for r in job_rs}
        active_names = {a.name for a in rows if a.ended_at is None}
        for a in rows:
            if a.ended_at is None or not (a.left_reason or "").startswith(STUCK_PREFIX):
                continue
            if a.agent_key in replaced or a.name in active_names:
                continue
            rec = enrolment_of(cfg, a, js.job)
            if rec is None:
                if unenrolled is not None and a.host == _host() and a.os_user == getpass.getuser():
                    unenrolled.append((js, a))
                continue
            c = _decide_for(board, cfg, sup, js, a, rows, job_rs, host_rs, now, running, rec)
            found.append(c)
            if c.decision.go:
                running += 1
    return found


def _invalid(board, job: str, agent_key: str, cfg: dict | None = None):
    """(why not, job status, agent row): why the agent must not be restarted now, read fresh from
    the board and (with `cfg`) its enrolment record (None: it still may), with what was read."""
    js = board.job_status(job)
    if js is None or js.status != "active":
        return "the job is closed", js, None
    if js.verdict == "met":
        return "the job has a met verdict", js, None
    if js.waiting_on and not js.waiting_on.startswith("supervisor: restarting "):
        return "the job is waiting on a human or external event", js, None
    if not js.supervise:
        return "supervise is off for the job", js, None
    from swarm.supervisor.orphans import human_question
    if human_question(board, js):
        return "the job has an unanswered owner question", js, None
    rows = board.agents(job)
    a = next((x for x in rows if x.agent_key == agent_key), None)
    if a is None or a.ended_at is None:
        return "its agent row is gone or active", js, a
    if any(x.name == a.name and x.ended_at is None for x in rows):
        return "its name is active again", js, a
    if any(r.old_agent_key == agent_key for r in board.restarts(job=job)):
        return "it was already replaced", js, a
    if cfg is not None and enrolment_of(cfg, a, job) is None:
        return NOT_ENROLLED, js, a
    return None, js, a


def _recheck(board, cfg: dict, sup: dict, c: Candidate, now):
    """The candidate decided again from a fresh read (this pass's restarts and launches, and
    every other supervisor's on this host, included), or (None, why not)."""
    why, js, a = _invalid(board, c.js.job, c.agent.agent_key, cfg)
    if why:
        return None, why
    rows = board.agents(js.job)
    return _decide_for(board, cfg, sup, js, a, rows, board.restarts(job=js.job), _host_restarts(board),
                       now or board.now(), _host_running(board), enrolment_of(cfg, a, js.job)), None


def workdir_for(cfg: dict, record) -> str | None:
    """The replacement's work directory: the cwd its enrolment record holds (the board's
    copy of the transcript and orchestrator markers are writable by agents, so never a source).
    Only an existing directory we can write and enter; never "/" and never a guess. None when
    there is no record or its directory is gone."""
    d = getattr(record, "cwd", None)
    if not d or not isinstance(d, str):
        return None
    p = Path(d)
    if p.is_dir() and str(p.resolve()) != "/" and os.access(p, os.W_OK | os.X_OK):
        return str(p)
    return None


def workdir_problem(cfg: dict, workdir: str) -> str | None:
    """Why a replacement may not run in `workdir`, or None. The
    work directory came from board data agents can forge (it now comes from the enrolment
    record; this stays as defence in depth), and a replacement trusts it (a Codex one may write all of it, a Claude one
    loads its project settings). So an allowlist: its real path lies under one of [supervise]
    allowed_workdirs (default ~/src), with no dot-directory below that root, is a directory of
    this user, and is never the home directory or above it."""
    import stat as _stat
    from swarm import paths
    from swarm.supervisor.settings import settings
    w = Path(os.path.realpath(workdir))
    bad = f"work dir {w} not under [supervise] allowed_workdirs"
    home = Path(os.path.realpath(paths.home()))
    if w == home or w in home.parents:
        return f"{bad} (it is the home directory or above it)"
    for r in settings(cfg)["allowed_workdirs"]:
        root = Path(os.path.realpath(os.path.expanduser(r)))
        if w != root and root not in w.parents:
            continue
        if any(part.startswith(".") for part in w.relative_to(root).parts):
            return f"{bad} (a dot-directory below {root})"
        try:
            st = os.stat(w)
        except OSError:
            return f"{bad} (it doesn't exist)"
        if not _stat.S_ISDIR(st.st_mode):
            return f"{bad} (not a directory)"
        if st.st_uid != compat.uid():
            return f"{bad} (it belongs to another user)"
        return None
    return bad


# ---- project configuration in a work dir and its approval store

APPROVALS = "approved.json"        # in the private directory (privfs): written by `supervise approve`
APPROVALS_LOCK = "approved.lock"
# what Claude or Codex load from a project (the work dir or a parent): a sandboxed agent that can
# write the work dir could plant any of them
CONFIG_ENTRIES = (".claude/settings.json", ".claude/settings.local.json", ".claude/hooks",
                  ".claude/agents", ".mcp.json", ".codex", "AGENTS.override.md")
CONFIG_MAX = 1024 * 1024           # bytes of one configuration file
TREE_MAX_ENTRIES = 2000            # entries of a configuration directory (hooks, agents, .codex)
TREE_MAX_DEPTH = 16
_HEX64 = re.compile(r"[0-9a-f]{64}")


class Unapprovable(ValueError):
    """Project configuration that is never approved: a symlink, FIFO, hard link, another user's
    file, or too big (the hash could not pin what the harness would load)."""


def _file_sha(d: int, name: str, where: str) -> str:
    import hashlib
    import stat as _stat
    fd = compat.open(name, os.O_RDONLY | compat.O_NOFOLLOW | compat.O_NONBLOCK | compat.O_CLOEXEC, dir_fd=d)
    try:
        st = os.fstat(fd)
        if not _stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != compat.uid():
            raise Unapprovable(f"{where} is not a regular file of this user with one link")
        if st.st_size > CONFIG_MAX:
            raise Unapprovable(f"{where} is larger than {CONFIG_MAX} bytes")
        h, n = hashlib.sha256(), 0
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            n += len(chunk)
            if n > CONFIG_MAX:
                raise Unapprovable(f"{where} is larger than {CONFIG_MAX} bytes")
            h.update(chunk)
        return h.hexdigest()
    finally:
        os.close(fd)


def _tree(fd: int, rel: bytes, where: str, h, seen: list, depth: int) -> None:
    import stat as _stat
    if depth > TREE_MAX_DEPTH:
        raise Unapprovable(f"{where} is nested too deep")
    for name in sorted(compat.listdir(fd)):
        seen.append(name)
        if len(seen) > TREE_MAX_ENTRIES:
            raise Unapprovable(f"{where} has more than {TREE_MAX_ENTRIES} entries")
        sub = rel + b"/" + os.fsencode(name)
        st = compat.stat(name, dir_fd=fd, follow_symlinks=False)
        if _stat.S_ISDIR(st.st_mode):
            h.update(b"d\0" + sub + b"\0")
            nfd = compat.open(name, os.O_RDONLY | compat.O_DIRECTORY | compat.O_NOFOLLOW | compat.O_CLOEXEC, dir_fd=fd)
            try:
                _tree(nfd, sub, f"{where}/{name}", h, seen, depth + 1)
            finally:
                os.close(nfd)
        elif _stat.S_ISREG(st.st_mode):
            h.update(b"f\0" + sub + b"\0" + _file_sha(fd, name, f"{where}/{name}").encode() + b"\0")
        else:
            raise Unapprovable(f"{where}/{name} is a symlink or not a regular file")


def _entry_sha(d: int, name: str, where: str) -> str | None:
    """The sha256 of configuration entry `name` in `d`: a regular file's content, or a directory's
    tree (names, kinds and file hashes); None if missing. Unapprovable for anything else."""
    import hashlib
    import stat as _stat
    try:
        st = compat.stat(name, dir_fd=d, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if _stat.S_ISREG(st.st_mode):
        return _file_sha(d, name, where)
    if _stat.S_ISDIR(st.st_mode):
        h = hashlib.sha256(b"tree\0")
        fd = compat.open(name, os.O_RDONLY | compat.O_DIRECTORY | compat.O_NOFOLLOW | compat.O_CLOEXEC, dir_fd=d)
        try:
            _tree(fd, b"", where, h, [], 1)
        finally:
            os.close(fd)
        return h.hexdigest()
    raise Unapprovable(f"{where} is a symlink or not a regular file or directory")


def config_hits(dir_fd: int, where: str) -> list[tuple[str, str]]:
    """[(relative path, sha256)] of the project configuration (CONFIG_ENTRIES) in the directory
    open as `dir_fd` (shown as `where`), read through descriptors only, never following a link
    and never blocking. Raises Unapprovable for a planted link, FIFO and the like."""
    import stat as _stat
    hits = []
    for rel in CONFIG_ENTRIES:
        parts = rel.split("/")
        d, opened = dir_fd, []
        try:
            for i, part in enumerate(parts[:-1]):
                try:
                    st = compat.stat(part, dir_fd=d, follow_symlinks=False)
                except FileNotFoundError:
                    d = None
                    break
                if _stat.S_ISLNK(st.st_mode):
                    raise Unapprovable(f"{where}/{'/'.join(parts[:i + 1])} is a symlink")
                if not _stat.S_ISDIR(st.st_mode):
                    d = None   # nothing is loaded from inside a non-directory
                    break
                d = compat.open(part, os.O_RDONLY | compat.O_DIRECTORY | compat.O_NOFOLLOW | compat.O_CLOEXEC, dir_fd=d)
                opened.append(d)
            if d is None:
                continue
            sha = _entry_sha(d, parts[-1], f"{where}/{rel}")
            if sha is not None:
                hits.append((rel, sha))
        finally:
            for fd in opened:
                os.close(fd)
    return hits


def workdir_config_problem(dir_fd: int, where: str, approvals) -> str | None:
    """Why a replacement may not run with the directory open as `dir_fd` (the work dir or one of
    its parents up to the allowed root, `where` its path) as project: project configuration in
    it that isn't approved with exactly this content (`approvals`: load_approvals()), or that
    can't be approved. None: nothing there, or all of it approved. Checks the opened inode, never
    a path."""
    try:
        hits = config_hits(dir_fd, where)
    except Unapprovable as exc:
        return f"{exc}: project configuration that is never approved (remove it)"
    except OSError as exc:
        return f"project configuration in {where} can't be checked ({exc.strerror or exc})"
    for rel, sha in hits:
        if (where, rel, sha) not in approvals:
            # the command first and the path once: board posts are capped, and macOS temp paths are long
            return (f"{rel}: unapproved project configuration; check it, then: "
                    f"swarm supervise approve {where}")
    return None


def _valid_entry(e) -> bool:
    return (isinstance(e, dict) and isinstance(e.get("dir"), str) and os.path.isabs(e["dir"])
            and os.path.normpath(e["dir"]) == e["dir"] and e.get("file") in CONFIG_ENTRIES
            and isinstance(e.get("sha256"), str) and bool(_HEX64.fullmatch(e["sha256"])))


def _raw_approvals(data: bytes | None) -> list[dict]:
    import json
    try:
        doc = json.loads(data) if data is not None else {}
    except ValueError:
        return []
    entries = doc.get("entries") if isinstance(doc, dict) else None
    return [e for e in entries if _valid_entry(e)] if isinstance(entries, list) else []


def load_approvals() -> set:
    """{(dir, file, sha256)} from the private approval store (privfs: never through a link); an
    empty set if it is missing, refused or unreadable (nothing is approved then)."""
    from swarm.supervisor.settings import read_private
    return {(e["dir"], e["file"], e["sha256"]) for e in _raw_approvals(read_private(APPROVALS))}


def approval_candidates(cfg: dict, directory: str) -> list[dict]:
    """What `swarm supervise approve <directory>` would approve: [{"dir", "file", "sha256"}] for
    every piece of project configuration in the directory and its parents up to the allowed root,
    read through the same descriptor walk the runner launches through. ValueError when the
    directory is not an allowed work dir, or holds configuration that can't be approved."""
    from swarm.supervisor import runner
    path = os.path.realpath(os.path.expanduser(str(directory)))
    why = workdir_problem(cfg, path)
    if why:
        raise ValueError(why)
    out: list[dict] = []

    def collect(fd: int, where: str):
        out.extend({"dir": where, "file": rel, "sha256": sha} for rel, sha in config_hits(fd, where))
        return None
    fd, why = runner.open_workdir(cfg, path, check=collect)
    if fd is None:
        raise ValueError(why)
    os.close(fd)
    return out


def save_approvals(entries: list[dict]) -> None:
    """Record `entries` (approval_candidates) in the private approval store, replacing earlier
    approvals of the same (dir, file), under its lock (privfs: a planted link at the store's name
    is replaced, never written through). ValueError for a malformed entry."""
    import json
    import time
    from swarm.supervisor import privfs
    from swarm.supervisor.settings import ensure_private_dir
    if not all(_valid_entry(e) for e in entries):
        raise ValueError("malformed approval entry")
    ensure_private_dir()
    with privfs.dir_fd() as d:
        lk = privfs.lock(d, APPROVALS_LOCK)
        try:
            compat.flock(lk, compat.LOCK_EX)
            keys = {(e["dir"], e["file"]) for e in entries}
            cur = [e for e in _raw_approvals(privfs.read(d, APPROVALS)) if (e["dir"], e["file"]) not in keys]
            now = time.time()
            cur += [{"dir": e["dir"], "file": e["file"], "sha256": e["sha256"], "at": now} for e in entries]
            privfs.write_atomic(d, APPROVALS, json.dumps({"v": 1, "entries": cur}, indent=1, sort_keys=True))
        finally:
            os.close(lk)


# ---- once-only posts (per host and job; the pass lock makes check-and-post one step)

def _post_once(board, state: dict, key: str, job: str, text: str) -> bool:
    from swarm.supervisor.stuck import SUPERVISOR_NAME
    posted = state.setdefault("posted", {})
    if key in posted:
        return False
    board.post(job, SUPERVISOR_NAME, text)
    posted[key] = {"job": job, "on": _dt.date.today().isoformat()}
    return True


def _note_once(state: dict, key: str, job: str, text: str) -> None:
    """Log `text` once (per job, like the once-only posts; forgotten when the job closes)."""
    from swarm.supervisor.settings import log
    posted = state.setdefault("posted", {})
    if key not in posted:
        posted[key] = {"job": job, "on": _dt.date.today().isoformat()}
        log(text)


def _cap_key(cap: str, job: str, lineage: str) -> str:
    if cap == "daily_minutes":
        return f"daily|{_host()}|{job}|{_dt.date.today()}"
    return f"cap|{_host()}|{job}|{lineage}|{cap}"


def _no_manager_key(job: str) -> str:
    return f"no-manager|{_host()}|{job}"


def _prune_posted(state: dict, open_jobs: set, manager_ok: bool | None) -> None:
    """Forget the once-only posts of jobs no longer open, daily caps of other days, and the
    no-manager notes once the manager answers (posted again if it goes again)."""
    posted = state.get("posted")
    if not isinstance(posted, dict):
        return
    today = _dt.date.today().isoformat()
    for key, v in list(posted.items()):
        v = v if isinstance(v, dict) else {}
        if v.get("job") not in open_jobs or (key.startswith("daily|") and v.get("on") != today) \
                or (key.startswith("no-manager|") and manager_ok):
            del posted[key]


def _clear_wait(board, job: str, name: str) -> None:
    from swarm.supervisor.stuck import WAITING_PREFIX
    cur = board.job_status(job)
    if cur is not None and cur.waiting_on == WAITING_PREFIX + name:
        board.set_waiting(job, None)


def clear_stale_waits(board) -> int:
    """The job's "supervisor: restarting <name>" wait flag is cleared once no
    restart of <name> can come any more: the job has a met verdict, supervise is off for it, or the
    name is active again. Otherwise it would stay set forever and keep the job from auto-closing.
    Other wait reasons are left alone. How many were cleared."""
    from swarm.supervisor.settings import log
    from swarm.supervisor.stuck import WAITING_PREFIX
    n = 0
    for js in board.jobs(False):
        w = js.waiting_on or ""
        if js.status != "active" or not w.startswith(WAITING_PREFIX):
            continue
        name = w[len(WAITING_PREFIX):]
        taken = any(a.name == name and a.ended_at is None for a in board.agents(js.job))
        why = ("the job has a verdict" if js.verdict == "met" else
               "supervise is off for the job" if not js.supervise else
               f"{name} is active again" if taken else None)
        if why and board.set_waiting(js.job, None):
            log(f"cleared the wait flag of {js.job} ({w}): {why}")
            n += 1
    return n


def _ensure_final_transcript(board, cfg: dict, js, a) -> bool | str:
    """The close stores the final transcript, later sweeps retry it; make sure it
    is there before the brief is built. Returns whether the stored transcript (if any) is only
    a non-final snapshot (the brief labels it). A failure is logged and never blocks the
    restart (with nothing stored the brief says so, and relies on the board). A capture-failed
    row that kept its snapshot: the label the brief uses instead of PARTIAL."""
    from swarm.supervisor import lost
    from swarm.supervisor.settings import log
    rows = []
    try:
        rows = board.transcripts(job=js.job, agent_key=a.agent_key)
        if rows and rows[0].final:
            from swarm.board.base import bodiless
            if rows[0].failed and not bodiless(rows[0]):   # the last snapshot; the brief says so
                return (f"final capture failed ({rows[0].failed}); last redacted snapshot from "
                        f"{rows[0].captured_at:%Y-%m-%d %H:%M} UTC")
            return False
        if lost.capture_final(board, cfg, js, a, time.monotonic() + BRIEF_CAPTURE_SECONDS):
            return False
        log(f"transcript of {a.name} ({a.agent_key}) on {js.job} not captured before restart")
    except Exception as exc:
        log(f"transcript of {a.name} ({a.agent_key}) on {js.job} not captured before restart: "
            f"{type(exc).__name__}")
    return bool(rows)


def clean_resume_markers(board, cfg: dict) -> int:
    """Leftovers of launches that failed: this machine+user's open restart rows without a run file
    (the runner never started; finishing them failed) are finished "failed"; resume markers
    without a run file (their runner never started, or finished all but the marker) are removed,
    their row finished "failed" if still open. A run file means its runner (or reap) owns it.
    Runs under the pass lock, so no launch of another pass is between its row, its marker and
    its run file. How many markers went."""
    from swarm.supervisor import markers, runner
    from swarm.supervisor.settings import log
    for r in board.restarts(host=_host(), os_user=getpass.getuser()):
        if r.ended_at is None and not runner.has_run(cfg, r.id):
            if board.finish_restart(r.id, "failed"):
                log(f"restart {r.id} on {r.job} never started (no run file): finished failed")
    mdir = Path(cfg["hook"]["marker_dir"]).expanduser()
    n = 0
    for p in sorted(mdir.glob("*--resume-r*.json")) if mdir.is_dir() else []:
        m = _RESUME_ID.search(p.name)
        if not m:
            continue
        rid = int(m.group(1))
        if runner.has_run(cfg, rid):
            continue
        # the marker directory is sandbox-writable: the marker's job is only a hint, and only a
        # restart row of this host and OS user, whose resume marker this is, is ever finished
        data = markers.read_marker(p)   # safefs: a FIFO or link planted here is never opened
        job = data.get("job") if isinstance(data, dict) else None
        row = next((r for r in board.restarts(job=job) if r.id == rid), None) if isinstance(job, str) else None
        if row is not None and (row.host != _host() or row.os_user != getpass.getuser()
                                or markers.resume_marker_path(cfg, row.job, rid).name != p.name):
            continue   # another host's or user's restart (or not this marker's): not ours to touch
        if row is not None and row.ended_at is None:
            board.finish_restart(rid, "failed")
            log(f"restart {rid} on {job} never started (no run file): finished failed")
        if markers.remove_resume_marker(p):
            n += 1
            log(f"removed leftover resume marker {p.name}")
        else:
            log(f"leftover resume marker {p.name} busy; retried next pass")
    return n


def _switched_off_now(cfg: dict, board=None, job: str | None = None) -> str | None:
    """A kill switch thrown since the pass began (the off files; with a board, the job's
    --no-supervise): why, or None. Read fresh."""
    from swarm.supervisor.settings import off_reason
    why = off_reason(cfg)
    if why:
        return f"the supervisor was switched off ({why})"
    if board is not None:
        js = board.job_status(job)
        if js is None or not js.supervise or js.status != "active":
            return "supervise is off for the job, or the job closed"
    return None


def _refuse(board, c: Candidate, reason: str, harness: str, why: str, max_per_job: int) -> None:
    from swarm.supervisor.settings import log
    from swarm.supervisor.stuck import SUPERVISOR_NAME
    a, job = c.agent, c.js.job
    if board.record_restart(job, c.lineage, a.agent_key, reason, harness, 0.0, outcome="refused",
                            max_per_job=max_per_job):
        board.post(job, SUPERVISOR_NAME, f"can't restart {a.name}: {why}")
        _clear_wait(board, job, a.name)
        log(f"refused restart of {a.name} on {job}: {why}. This is permanent: the agent is not "
            f"retried (the refused row counts as its replacement); restart it by hand if needed")


def _act(board, cfg: dict, sup: dict, c: Candidate, state: dict, *, start_runner, which,
         scope_ok, config_path: str) -> bool:
    """Act on one (freshly decided) candidate; True if a replacement was launched."""
    from swarm.board.autoinit import store_key
    from swarm.supervisor import brief as br, launch, markers, runner
    from swarm.supervisor.settings import log
    from swarm.supervisor.stuck import SUPERVISOR_NAME
    a, js, d = c.agent, c.js, c.decision
    if not d.go:
        if d.cap:
            _post_once(board, state, _cap_key(d.cap, js.job, c.lineage), js.job,
                       f"GAVE UP {a.name}: {d.why}" if d.cap == "agent" else f"not restarting {a.name}: {d.why}")
            _clear_wait(board, js.job, a.name)
            log(f"not restarting {a.name} on {js.job}: {d.why}")
        else:   # a wait (backoff, concurrency): it clears by itself; logged, not posted
            log(f"not restarting {a.name} on {js.job} (for now): {d.why}")
        return False
    if not scope_ok():
        # the runner would only refuse; record nothing, post once per host and job
        if _post_once(board, state, _no_manager_key(js.job), js.job, NO_MANAGER_NOTE):
            log(f"{NO_MANAGER_NOTE} ({a.name} on {js.job})")
        _clear_wait(board, js.job, a.name)
        return False
    rec = c.record
    harness = rec.harness if rec is not None else None
    reason = "outage" if c.outage else a.left_reason
    max_job = int(sup["max_restarts_per_job"])
    partial = _ensure_final_transcript(board, cfg, js, a)
    b = br.build_brief(board, cfg, js, a, a.left_reason, d.attempt, int(sup["max_restarts_per_agent"]),
                       partial=partial)
    workdir = workdir_for(cfg, rec)
    binary = sup.get(f"{harness}_bin") if harness in ("claude", "codex") else None
    row_harness = a.harness or "claude"
    if binary and not which(binary):
        # A shared host may install each harness for a different OS user. Hold until
        # this user's harness is available; do not spend a permanent refusal attempt.
        why = f"host executable unavailable for {harness}"
        if _post_once(board, state, f"executable|{_host()}|{js.job}|{harness}", js.job,
                      f"not restarting {a.name}: {why}"):
            log(f"not restarting {a.name} on {js.job}: {why}")
        _clear_wait(board, js.job, a.name)
        return False
    why = (None if rec is not None else NOT_ENROLLED) or \
          (None if row_harness == harness else
           f"its board row's harness {row_harness!r} is not the one it was enrolled with here "
           f"({harness!r})") or \
          (None if binary else f"its recorded harness {harness!r} is unknown") or \
          (None if workdir else "its work directory is unknown or gone") or \
          (workdir_problem(cfg, workdir) if workdir else None)
    if not why and workdir:   # project configuration: wait (posted once) until approved, record nothing
        config_why = _workdir_hold(cfg, workdir)
        if config_why:
            if _post_once(board, state, f"config|{_host()}|{js.job}|{a.agent_key}|{config_why}", js.job,
                          f"not restarting {a.name}: {config_why}"):
                log(f"not restarting {a.name} on {js.job}: {config_why}")
            _clear_wait(board, js.job, a.name)
            return False
    stale, _, _ = _invalid(board, js.job, a.agent_key, cfg)   # right before recording: still restartable?
    stale = stale or _switched_off_now(cfg)
    if stale:
        log(f"not restarting {a.name} on {js.job}: {stale}")
        clear_stale_waits(board)   # a verdict or --no-supervise since the pass began
        return False
    if why:
        _refuse(board, c, reason, harness or row_harness, why, max_job)
        return False
    workdir = os.path.realpath(workdir)   # only the checked real path goes to the runner
    model = launch.replacement_model(cfg, harness, a.role, a.model)
    from swarm.supervisor.settings import today_start
    # every cap the decision used, checked again with the insert (atomically: other passes, other
    # OS users' timers and other hosts may have recorded restarts since)
    r = board.record_restart(js.job, c.lineage, a.agent_key, reason, harness, d.minutes, max_per_job=max_job,
                             max_job_minutes=float(sup["max_restart_minutes"]),
                             max_host_running=int(sup["max_concurrent_replacements"]),
                             max_host_minutes=float(sup["daily_restart_minutes"]), day_start=today_start())
    if r is None:   # replaced meanwhile, or a cap was reached meanwhile: next pass sees which
        log(f"not restarting {a.name} on {js.job} (for now): already replaced, or a cap was reached "
            f"meanwhile (the job's restarts or minutes, or this host's running replacements or "
            f"minutes); checked again next pass")
        return False
    marker = None
    launched = False
    try:
        spec = launch.spec_for(cfg, harness, prompt=b.text, workdir=workdir, model=model, minutes=d.minutes)
        marker = markers.write_resume_marker(cfg, js.job, r.id, resume_of=a.agent_key, name=a.name,
                                             harness=harness, session_id=spec.session_id)
        if spec.session_id:
            board.set_restart_agent(r.id, spec.session_id)
        off = _switched_off_now(cfg, board, js.job)   # right before the launch
        if off:
            board.finish_restart(r.id, "cancelled")
            _clear_wait(board, js.job, a.name)
            log(f"restart {r.id} of {a.name} on {js.job} cancelled before its launch: {off}")
            return False
        run = {"restart_id": r.id, "job": js.job, "name": a.name, "harness": harness,
               "resume_of": a.agent_key, "marker": str(marker), "argv": list(spec.argv), "cwd": spec.cwd,
               "stdin": spec.stdin, "session_id": spec.session_id, "limit_seconds": d.minutes * 60,
               "enrol_seconds": float(sup["enrol_minutes"]) * 60, "config": config_path,
               "board": store_key(cfg)}
        # posted before the runner starts: a replacement that ends at once posts its end after
        board.post(js.job, SUPERVISOR_NAME,
                   f"restarted {a.name} (attempt {d.attempt}/{sup['max_restarts_per_agent']}): "
                   f"{a.left_reason}; {model or 'default model'}, up to {d.minutes:.0f} min")
        (start_runner or runner.start)(cfg, run)
        launched = True
    except Exception as exc:
        log(f"restart {r.id} of {a.name} on {js.job} failed to start: {type(exc).__name__}: {exc}")
        try:
            board.finish_restart(r.id, "failed")
            board.post(js.job, SUPERVISOR_NAME, f"restart of {a.name} failed to start ({type(exc).__name__})")
            _clear_wait(board, js.job, a.name)
        except Exception as exc2:
            log(f"restart {r.id}: finishing it failed ({type(exc2).__name__}); the next pass finishes it")
    finally:
        if not launched and marker is not None:
            try:
                gone = markers.remove_resume_marker(marker)
            except Exception:
                gone = False
            if not gone:
                log(f"resume marker {marker.name} of failed restart {r.id} not removed; retried next pass")
    if not launched:
        return False
    log(f"restarted {a.name} on {js.job} as restart {r.id} (attempt {d.attempt}), {harness}, "
        f"{model or 'default model'}, {d.minutes:.0f} min")
    return True


def _workdir_hold(cfg: dict, workdir: str) -> str | None:
    """Why the work dir can't be launched in now, as the runner will open it (unapproved project
    configuration, or a path problem since the pass checked it): the pass waits on it (posted
    once, nothing recorded), and --dry-run shows it. Read-only: opens directories, writes
    nothing."""
    from swarm.supervisor import runner
    fd, why = runner.open_workdir(cfg, os.path.realpath(workdir))
    if fd is not None:
        os.close(fd)
    return why


def _caps_lines(board, sup: dict, job: str | None, running: int) -> list[str]:
    from swarm.supervisor import budget
    now = board.now()
    used = sum(budget.charged_minutes(r, now) for r in _host_restarts(board))
    lines = [f"caps: daily {used:.0f}/{sup['daily_restart_minutes']} min on this host, running "
             f"{running}/{sup['max_concurrent_replacements']}, per agent {sup['max_restarts_per_agent']}, "
             f"per replacement {sup['max_minutes']} min / {sup['max_turns']} turns"]
    for js in board.jobs(False):
        if job and js.job != job:
            continue
        rs = board.restarts(job=js.job)
        if rs or not js.supervise:
            lines.append(f"caps {js.job}: " + ("supervise off (--no-supervise)" if not js.supervise else
                         f"restarts {len(rs)}/{sup['max_restarts_per_job']}, minutes "
                         f"{sum(budget.charged_minutes(r, now) for r in rs):.0f}/{sup['max_restart_minutes']}"))
    return lines


def _unreachable(dry_run: bool, say) -> int:
    from swarm.supervisor import outage
    from swarm.supervisor.settings import log
    if dry_run:
        say("board unreachable: nothing restarted (dry run: outage not noted)")
        return 0
    outage.note_unreachable()
    log("board unreachable: nothing done (outage noted)")
    say("board unreachable: nothing restarted (outage noted)")
    return 0


NOT_INITIALISED = "board not initialised: nothing to supervise"
# A connection pooler answers a connection to a missing database with this
# instead of Postgres's "database ... does not exist": it may be either
POOLER_HIDDEN = "unable to get session context"
MAYBE_NOT_INITIALISED = "board unreachable or not initialised (a connection pooler hides which): nothing to supervise"


def _store_missing(cfg: dict) -> bool:
    """Whether the board's store doesn't exist yet (a dry run then opens nothing: the file
    backend's constructor would create its directory and lock file). Read-only checks: the file
    board's directory with its lock and schema_version files; the SQLite file. Postgres and
    memory: opening creates nothing (a missing Postgres database fails to connect: see
    _missing_database)."""
    backend = board_backend(cfg)
    if backend == "file":
        from swarm.board import file as fb
        d = fb.board_dir(cfg)
        return not (d.is_dir() and (d / fb._LOCK).is_file() and (d / fb._VERSION).is_file())
    if backend == "sqlite":
        from swarm.board import sqlite as sb
        return not sb.db_path(cfg).is_file()
    return False


def _missing_database(exc: Exception) -> bool:
    """A Postgres connection refused because the board database doesn't exist."""
    text = f"{exc} {exc.__cause__ or ''}"
    return "does not exist" in text and "database" in text


def _open(cfg: dict, dry_run: bool):
    """The board. A dry run opens it read-only: no recovery setup of a missing store, and the
    file and SQLite backends create, write, truncate and rename nothing (board.open_read_only)."""
    from swarm.board import open_board, open_read_only
    return open_read_only(cfg) if dry_run else open_board(cfg)


def _dry_run_hold(cfg: dict, c: Candidate) -> str | None:
    """What the real pass would wait on for this candidate (see _act): its work dir's project
    configuration check, run read-only (a dry run never says "would restart" for an
    agent the pass will hold)."""
    workdir = workdir_for(cfg, c.record)
    if not workdir or workdir_problem(cfg, workdir):
        return None   # refused, not held: the real pass says why
    try:
        return _workdir_hold(cfg, workdir)
    except Exception as exc:   # a dry run reports, never fails on it
        return f"its work dir can't be checked ({type(exc).__name__})"


def _dry_run(board, cfg: dict, sup: dict, job, now, say, scope_ok, config_path="") -> None:
    from swarm.supervisor import runner, stuck
    for js, a, why in stuck.find_stuck_owned(board, cfg, now):
        if not job or js.job == job:
            say(f"would close {a.name} on {js.job}: stuck:{why}")
    from swarm.supervisor import orphans, pipeline
    from swarm.supervisor.settings import load_state
    state = load_state()
    managed = pipeline.run(board, cfg, sup, state, job=job, now=now, dry_run=True, say=say, scope_ok=scope_ok, config_path=config_path)
    orphans.run(board, cfg, sup, state, job=job, now=now, dry_run=True, say=say, scope_ok=scope_ok, skip_jobs=managed)
    running = _host_running(board)
    unenrolled: list = []
    for c in candidates(board, cfg, sup, job=job, now=now, running=running, unenrolled=unenrolled):
        if c.js.job in managed:
            continue
        d = c.decision
        if d.go and not scope_ok():
            say(f"not restarting {c.agent.name} on {c.js.job}: no user systemd manager "
                f"(fix: loginctl enable-linger $USER)")
        elif d.go and (hold := _dry_run_hold(cfg, c)):
            say(f"would hold {c.agent.name} on {c.js.job} for approval (nothing recorded): {hold}")
        elif d.go:
            say(f"would restart {c.agent.name} on {c.js.job} (attempt {d.attempt}/"
                f"{sup['max_restarts_per_agent']}, up to {d.minutes:.0f} min): {c.agent.left_reason}")
        else:
            say(f"not restarting {c.agent.name} on {c.js.job}: {d.why}")
    for js, a in unenrolled:
        say(f"not restarting {a.name} on {js.job}: {NOT_ENROLLED}")
    for line in _caps_lines(board, sup, job, running):
        say(line)


ENROLMENT_MIN_DAYS = 7


def prune_enrolments(board, cfg: dict, now: float | None = None) -> int:
    """Drop this board's enrolment records older than max([transcripts]
    retention_days, ENROLMENT_MIN_DAYS) days whose job is closed or gone (they outlive the final
    transcript and the supervisor's window by then); records of active jobs and of other boards
    are kept (those boards' passes decide). Junk in the records dir goes too. How many removed."""
    from swarm import enrolment
    from swarm.board.autoinit import store_key
    try:
        days = float((cfg.get("transcripts") or {}).get("retention_days", 30))
    except (TypeError, ValueError):
        days = 30.0
    days = max(days, ENROLMENT_MIN_DAYS)
    here = store_key(cfg)
    active: dict = {}

    def keep(rec) -> bool:
        if rec.board_key != here:
            return True
        if rec.job not in active:
            js = board.job_status(rec.job)
            active[rec.job] = js is not None and js.status == "active"
        return active[rec.job]
    return enrolment.prune(days * 86400.0, now=now, keep=keep)


def _pass(board, cfg: dict, sup: dict, state: dict, job, now, say, start_runner, which, scope_ok,
          config_path: str, began: float | None = None) -> None:
    """`began`: time.monotonic() when the pass started (run_pass, before the board was opened):
    PASS_BUDGET_SECONDS counts from there."""
    from swarm import cli
    from swarm.supervisor import outage, runner
    began = time.monotonic() if began is None else began
    outage.note_reachable()
    runner.reap(cfg, board)
    clean_resume_markers(board, cfg)
    from swarm.supervisor import orphans, pipeline
    managed = pipeline.run(board, cfg, sup, state, job=job, now=now, say=say,
                           start_runner=start_runner, which=which, scope_ok=scope_ok, config_path=config_path)
    orphans.run(board, cfg, sup, state, job=job, now=now, say=say,
                start_runner=start_runner, which=which, scope_ok=scope_ok, config_path=config_path, skip_jobs=managed)
    cli.sweep_jobs(board, cfg, time.monotonic() + PASS_SWEEP_SECONDS)
    clear_stale_waits(board)
    unenrolled: list = []
    found = candidates(board, cfg, sup, job=job, now=now, running=_host_running(board), unenrolled=unenrolled)
    for js, a in unenrolled:
        _note_once(state, f"unenrolled|{js.job}|{a.agent_key}", js.job,
                   f"not restarting {a.name} ({a.agent_key}) on {js.job}: {NOT_ENROLLED}")
    for c in found:
        if c.js.job in managed:
            continue
        fresh, why = _recheck(board, cfg, sup, c, now)   # this pass's launches count (open rows)
        if fresh is None:
            from swarm.supervisor.settings import log
            log(f"not restarting {c.agent.name} on {c.js.job}: {why}")
            continue
        _act(board, cfg, sup, fresh, state, start_runner=start_runner, which=which,
             scope_ok=scope_ok, config_path=config_path)
    try:
        n = prune_enrolments(board, cfg)
        if n:
            from swarm.supervisor.settings import log
            log(f"pruned {n} old enrolment record(s) of closed jobs")
    except Exception as exc:   # housekeeping: never fails a pass
        from swarm.supervisor.settings import log
        log(f"pruning enrolment records failed ({type(exc).__name__})")
    try:   # last: the pending finals the sweeps left to the pass, in what is left of its budget
        from swarm.supervisor import lost
        lost.retry_slow_finals(board, cfg, began + PASS_BUDGET_SECONDS)
    except Exception as exc:   # never fails a pass
        from swarm.supervisor.settings import log
        log(f"retrying pending final transcripts failed ({type(exc).__name__})")
    for line in _caps_lines(board, sup, job, _host_running(board)):
        say(line)


def run_pass(cfg: dict, *, dry_run: bool = False, job: str | None = None, say=print,
             start_runner=None, which=shutil.which, now=None, scope_available=None,
             config: str | None = None) -> int:
    """One pass. `start_runner(cfg, run)` (runner.start), `which` and `scope_available`
    (runner.scope_available) are injectable; `config`: the config file the runner loads
    (default: $SWARM_CONFIG or ~/.config/swarm/config.toml)."""
    began = time.monotonic()   # the pass's budget (PASS_BUDGET_SECONDS) counts from here
    from swarm import paths
    from swarm.board import BoardUnavailable
    from swarm.supervisor import runner
    from swarm.supervisor.settings import (PrivateDirError, SettingsError, ensure_private_dir, load_state,
                                           save_state, settings, switched_off)
    try:
        sup = settings(cfg)
        from swarm.supervisor import pipeline
        pipeline.settings(cfg)
    except (SettingsError, ValueError) as exc:
        print(f"swarm supervise: {exc}", file=sys.stderr)
        return 1
    if not sup["enabled"]:
        say("supervisor disabled ([supervise] enabled = false): nothing to do")
        return 0
    off = switched_off()
    if off is not None:
        say(f"supervisor off ({off} exists): nothing to do")
        return 0
    try:
        ensure_private_dir(cfg, create=not dry_run)
    except (PrivateDirError, OSError) as exc:
        print(f"swarm supervise: {exc}", file=sys.stderr)
        return 1
    refusal = exposure_refusal()
    if refusal:
        if not dry_run:
            from swarm.supervisor.settings import log
            log(refusal)
        say(refusal)
        print(f"swarm supervise: {refusal}", file=sys.stderr)
        return 1
    probe = scope_available or runner.scope_available
    scope: list[bool] = []

    def scope_ok() -> bool:   # asked at most once per pass (it runs systemctl)
        if not scope:
            scope.append(bool(probe()))
        return scope[0]

    if dry_run:   # opens every backend read-only; a missing store is never created
        if _store_missing(cfg):
            say(NOT_INITIALISED)
            return 0
        try:
            with _open(cfg, True) as board:
                _dry_run(board, cfg, sup, job, now, say, scope_ok, config or str(paths.config_path()))
        except BoardUnavailable as exc:
            if _missing_database(exc):
                say(NOT_INITIALISED)
                return 0
            if POOLER_HIDDEN in f"{exc} {exc.__cause__ or ''}":
                say(MAYBE_NOT_INITIALISED)
                return 0
            return _unreachable(True, say)
        return 0

    lock = _take_pass_lock()
    if lock is None:
        say(BUSY)
        return 0
    try:
        state = load_state()
        try:
            with _open(cfg, False) as board:
                from swarm.supervisor import lost
                with lost.pass_writes():   # the retry state is written even if its lock is held
                    _pass(board, cfg, sup, state, job, now, say, start_runner, which, scope_ok,
                          config or str(paths.config_path()), began)
                _prune_posted(state, {js.job for js in board.jobs(False)}, scope[0] if scope else None)
        except BoardUnavailable:
            return _unreachable(False, say)
        finally:
            state["last_run_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat()
            save_state(state)
    finally:
        os.close(lock)
    return 0


def cmd_supervise(cfg: dict, args) -> int:
    return run_pass(cfg, dry_run=args.dry_run, job=args.job, config=str(args.config))
