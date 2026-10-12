"""Person authority and caller identity for drive control commands (spec 3.4 [G6], [F17], 4.0, 4.3 [F5]).

Who runs a control command is never taken from the environment: CLAUDE_CODE_SESSION_ID / CODEX_* are set by
whoever starts the process, and an agent can export another session's id or unset its own (`env -u`). The
caller comes from a **grant**, a host-private receipt (swarm.receipts, kind "grant") that the unsandboxed
PreToolUse hook writes before the command runs (drivegate.write_grants), from the same contexts as Task 5's
receipts: the job's requester MAIN session (requester_context: the ledger's requester_session, no agent_id),
or the seat the host-private chain names (seat_context: marker, run file, ledger intent). It is bound to
args_sha of the submitted argv (drivegate.submitted_argv, computed here from the parsed arguments), to the
job's incarnation and generation, lives GRANT_SECONDS, and is used up by the first command that takes it
(receipts.take). A subagent never gets one. A Codex seat cannot write one (its sandbox is the boundary) nor
use one up (the unlink fails in the sandbox). A same-user unsandboxed Claude seat could write one itself: that is the
guardrail line of spec 4.0 [F24].

Person authority (halt, unhalt, seat cancel, a person's seat request, drive repair) is held by exactly two
callers ([G6] reconciling [F17]):
  (a) the job's requester session, by its grant;
  (b) a person at a terminal: no grant, no agent host around the process (no host session variables and no
      claude/codex ancestor process), not an enrolled session, and a `y` typed on /dev/tty.
Every other caller is refused, in particular any process under an agent host without a grant (an agent that
unset its session variables, a nested shell the hook cannot read, a subagent). The tty path is a guardrail,
not a boundary, against a same-user agent (spec 4.0): an agent that escapes its process tree (setsid under
another parent) and drives a pty it owns can still answer. The grant authorizes an agent host's caller; the
tty is defence-in-depth for a human outside any agent host."""
from __future__ import annotations

import os
import time
from dataclasses import dataclass

from swarm import enrolment, hosts

# Variables an agent host sets for the processes it runs. Their presence means "an agent host is around"; their
# absence means nothing (an agent can unset them), so the ancestor walk is the second check.
HOST_ENV = ("CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT",
            "CODEX_SESSION_ID", "CODEX_THREAD_ID", "CODEX_SANDBOX", "CODEX_SANDBOX_NETWORK_DISABLED")


class PersonRefused(Exception):
    """A control command the caller may not run; the message says why and what to do instead."""


@dataclass(frozen=True)
class Caller:
    """Who the hook attested for this command: the job's requester session (seat None), or a seat by name."""
    seat: str | None
    action_id: str

    @property
    def requester(self) -> bool:
        return self.seat is None and self.action_id.startswith("requester:")


_TAKEN: dict = {}
# A jailed seat's broker (Task 20) attests the commands it runs for the seat with a per-call SECRET (env), whose
# record (the seat's session pid, an expiry) only the broker writes, in the supervisor's private dir, under the
# secret's hash. A bare variable an agent sets attests nothing: without the record it names, it is ignored (rr19).
BROKER_TOKEN_ENV = "SWARM_BROKER_TOKEN"
BROKER_ATTEST_DIR = "broker-attest"
_ATTESTED: dict = {}


def broker_attest_name(token: str) -> str:
    import hashlib
    return "attest-" + hashlib.sha256(token.encode()).hexdigest() + ".json"


def broker_attested(env=None, attest_dir=None) -> int | None:
    """The seat session pid the broker attested for this process (BROKER_TOKEN_ENV names a live record the broker
    wrote), else None. Never raises."""
    import json
    import re
    env = os.environ if env is None else env
    token = env.get(BROKER_TOKEN_ENV) or ""
    if not re.fullmatch(r"[0-9a-f]{64}", token):
        return None
    if token in _ATTESTED:
        return _ATTESTED[token]
    pid = None
    try:
        from swarm import safefs
        from swarm.supervisor.settings import private_dir
        base = attest_dir or str(private_dir() / BROKER_ATTEST_DIR)
        fd = safefs.open_base(base, create=False, strict_mode=0o700)
        try:
            raw = safefs.read(fd, broker_attest_name(token))
        finally:
            os.close(fd)
        rec = json.loads(raw) if raw else None
        if isinstance(rec, dict) and isinstance(rec.get("pid"), int) and rec["pid"] > 1 \
                and float(rec.get("expires_at", 0)) > time.time():
            pid = rec["pid"]
    except Exception:
        pid = None
    _ATTESTED[token] = pid
    return pid


def host_pid(parent_of=None, pid: int | None = None, max_hops: int = 40) -> int | None:
    """The pid of the OUTERMOST claude/codex process above this one (the agent session's own process: Codex
    runs a command under a `codex-*` sandbox helper, below the codex process its hooks run under), or None.
    The hook and the CLI of one tool call find the same pid, so a grant bound to it is no use to a process of
    another session or to one outside every agent host."""
    if pid is None and parent_of is None:
        brokered = broker_attested()
        if brokered is not None:
            # a jailed seat's hook or command, run outside the jail by its runner's broker (seatbroker): no
            # claude/codex process is above it here, so the broker's record names the seat's own session process
            return brokered
    if parent_of is None:
        from swarm.supervisor import procwatch
        parent_of = procwatch.parent_of        # /proc, `ps` (macOS), a Toolhelp32 snapshot (Windows)
    pid = os.getppid() if pid is None else pid
    found = None
    for _ in range(max_hops):
        if pid <= 1:
            break
        try:
            info = parent_of(pid)
        except Exception:
            break
        if not info:
            break
        ppid, argv = info
        if hosts._classify(argv):
            found = pid
        pid = ppid
    return found


def forget() -> None:
    """Start a new command: the next caller() takes a grant again (drive_cli calls it per handler)."""
    _TAKEN.clear()


def caller(board_key: str, job: str, led, args, now: float | None = None) -> Caller | None:
    """The caller the hook attested for this exact command (`args`, the parsed arguments) of `job` (`led`: its
    ledger, None when it does not read), or None. The grant is used up on the first call; later calls in the
    same command return the same answer."""
    from swarm import drivegate, receipts
    rid = drivegate.grant_id(args, host_pid())
    key = (board_key, job, rid)
    if key in _TAKEN:
        return _TAKEN[key]
    inc, gen = ((led.incarnation, led.generation) if led is not None
                else (receipts.GRANT_INCARNATION_NO_LEDGER, 1))
    r = receipts.take(board_key, job, "grant", rid, rev=1, incarnation=inc, generation=gen,
                      now=time.time() if now is None else now)
    who = None
    if r is not None and r.content_sha == receipts.content_sha("grant", {"argv_sha": rid}) and r.args_sha == rid:
        who = Caller(r.seat, r.action_id)
    _TAKEN[key] = who
    return who


CODEX_ENV = ("CODEX_THREAD_ID", "CODEX_SESSION_ID", "CODEX_SANDBOX", "CODEX_SANDBOX_NETWORK_DISABLED")


def codex_session(env, ancestor=None) -> bool:
    """Whether this command runs in a Codex session: a Codex variable is set, or the nearest agent host above
    the process is codex. Such a command cannot use a grant (its sandbox cannot touch the enrolled
    directory), so its ledger-writing control commands go to the driver as CONTROL-REQUESTs."""
    if broker_attested(env) is not None:
        return False   # run by a jailed seat's broker outside the jail: the broker attests the caller (its grant)
    if any(env.get(k) for k in CODEX_ENV):
        return True
    if any(env.get(k) for k in ("CLAUDE_CODE_SESSION_ID", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")):
        return False
    try:
        host = (ancestor or (lambda: hosts.nearest_ancestor_host(hosts._proc_parent, os.getppid())))()
    except Exception:
        return False
    return host == "codex"


def _ancestor_host() -> str | None:
    """The nearest claude/codex process above this one, or None when the ancestry was read up to the top and
    has none. Linux /proc, macOS `ps`, Windows a Toolhelp32 snapshot (procwatch.parent_of). "unknown" when it
    cannot be read: treated as inside an agent host (fails closed), on every platform."""
    from swarm.supervisor import procwatch
    try:
        return procwatch.ancestor_host(os.getppid())
    except Exception:
        return "unknown"


def agent_host(env, ancestor=None) -> str | None:
    """Why this process runs under an agent host (a host variable, or a claude/codex ancestor), else None."""
    found = [k for k in HOST_ENV if env.get(k)]
    if found:
        return f"{found[0]} is set"
    host = (ancestor or _ancestor_host)()
    return f"it runs under a {host} process" if host else None


# --- the tty --------------------------------------------------------------------------------------------

def _open_tty() -> int:
    """The controlling terminal as one read-write descriptor. Unbuffered: a tty is not seekable, so a text-mode
    file object opened "r+" raises on it (the 6b fault: the prompt never confirmed on a real terminal)."""
    return os.open("/dev/tty", os.O_RDWR | getattr(os, "O_NOCTTY", 0))


def _tty_confirm(action: str, job: str) -> bool:
    """Ask on the controlling terminal; only a line reading `y` or `yes` confirms. No terminal: no."""
    try:
        fd = _open_tty()
    except OSError:
        return False
    try:
        os.write(fd, f"swarm: {action} job {job}? type y or yes to confirm: ".encode("utf-8", "replace"))
        line = b""
        while not line.endswith(b"\n") and len(line) < 64:
            chunk = os.read(fd, 1)
            if not chunk:
                break
            line += chunk
        return line.decode("utf-8", "replace").strip().lower() in ("y", "yes")
    except OSError:
        return False
    finally:
        os.close(fd)


def session_of(env) -> str | None:
    """The host session id the environment claims (for messages only: never an authority)."""
    return hosts.cli_session_id(env)


# --- the gate ------------------------------------------------------------------------------------------

def require(action: str, job: str, *, board_key: str, who: Caller | None, env=None, confirm=None,
            ancestor=None) -> str:
    """Return who may act: "requester" (the job's requester main session, by its grant) or "tty" (a person at
    a terminal outside any agent host, confirmed on /dev/tty). PersonRefused otherwise. `who` is caller()'s
    answer; `confirm(action, job)` replaces the tty prompt and `ancestor()` the process-tree walk (tests)."""
    env = os.environ if env is None else env
    if who is not None:
        if who.requester:
            return "requester"
        raise PersonRefused(f"refused: {action} is a person's action, and this session (seat {who.seat}) is not "
                            f"the requester session of {job}. Ask the job's lead (--to @EL) or a person.")
    sid = session_of(env)
    if sid and sid in enrolment.agent_sessions(board_key):
        raise PersonRefused(f"refused: {action} is a person's action; this is an enrolled agent session, "
                            "and enrolled sessions never run it. Ask the job's lead (--to @EL) or a person.")
    inside = agent_host(env, ancestor)
    if inside:
        raise PersonRefused(f"refused: {action} needs a person, and this process runs inside an agent host "
                            f"({inside}) with no hook grant for this command. The requester session runs it as "
                            "a plain `swarm ...` command from its main session (its hook attests it); a person "
                            "runs it from a terminal outside any agent.")
    if (confirm or _tty_confirm)(action, job):
        return "tty"
    raise PersonRefused(f"refused: {action} needs a person: run it from the session that activated {job}, "
                        "or from a terminal and confirm on the tty.")
