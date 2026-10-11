"""Person authority and caller identity for drive control commands (spec 3.4 [G6], [F17], 4.0, 4.3 [F5]).

Who runs a control command is never taken from the environment: CLAUDE_CODE_SESSION_ID / CODEX_* are set by
whoever starts the process, and an agent can export another session's id or unset its own (`env -u`). The
caller's identity comes from a **grant**: the unsandboxed PreToolUse hook sees the agent's shell call with the
host's own payload (`session_id`, and `agent_id` for a subagent), finds the direct `swarm` invocations in it,
and writes one single-use grant per invocation into the host-private enrolled directory
(`grant-<sha256(board key, argv)>.json`, 0600 under a 0700 directory, safefs rules). The CLI then takes the
grant of its own argv (read, then unlink: one use, `GRANT_SECONDS` old at most). A Codex seat cannot write
there (its sandbox is the boundary), and cannot consume one either (the unlink fails in the sandbox). A
same-user unsandboxed Claude seat could forge one: that is the guardrail line of spec 4.0 [F24].

Person authority (halt, unhalt, seat cancel, a person's seat request, drive repair) is held by exactly two
callers ([G6] reconciling [F17]):
  (a) the job's requester session: a grant whose session is the one recorded in the job's owner record, from
      its MAIN session (no agent_id: Agent-tool subagents share the parent's session id and are never the
      requester, spec [F17]);
  (b) a person at a terminal: no grant, no agent host around the process (no host session variables and no
      claude/codex ancestor process), not an enrolled session, and a `y` typed on /dev/tty.
Every other caller is refused, in particular any process that runs under an agent host without a grant (an
agent that unset its session variables, a nested shell the hook cannot read, a subagent). The tty path is a
guardrail, not a boundary, against a same-user agent (spec 4.0): an agent that escapes its process tree
(setsid under another parent) and drives a pty it owns can still answer. The grant is what authorizes an
agent host's caller; the tty is defence-in-depth for a human outside any agent host."""
from __future__ import annotations

import hashlib
import json
import os
import shlex
import sys
import time
from dataclasses import dataclass

from swarm import compat, enrolment, hosts, safefs

GRANT_SECONDS = 120.0
GRANT_MAX = 16 * 1024
GRANT_FORMAT = 1
# the swarm sub-commands whose caller identity a handler asks for (drive_cli): only these get a grant
GRANTED = {"halt", "unhalt", "seat", "goal-ready", "drive"}
_PUNCT = "();<>|&\n"
# Variables an agent host sets for the processes it runs. Their presence means "an agent host is around"; their
# absence means nothing (an agent can unset them), so the ancestor walk is the second check.
HOST_ENV = ("CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT",
            "CODEX_SESSION_ID", "CODEX_THREAD_ID", "CODEX_SANDBOX", "CODEX_SANDBOX_NETWORK_DISABLED")


class PersonRefused(Exception):
    """A control command the caller may not run; the message says why and what to do instead."""


@dataclass(frozen=True)
class Caller:
    """Who the hook saw submit this exact command: the host session, and the subagent id when a subagent did."""
    session_id: str
    agent_id: str | None


# --- the hook side: grants ---------------------------------------------------------------------------

def _is_punct(tok: str) -> bool:
    return bool(tok) and all(c in _PUNCT for c in tok)


def swarm_calls(command: str) -> list[list[str]]:
    """The argv (after the `swarm` word) of each direct `swarm` invocation in a shell command: a command word
    whose base name is `swarm`, optionally after VAR=value assignments. Anything the hook cannot read exactly
    (substitutions, quoting it cannot split) yields no argv, so its command gets no grant (fails closed). When
    an argv ends in a number just before a redirection (`2>&1`), both readings are returned."""
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=_PUNCT)
        lex.whitespace = " \t\r"
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:
        return []
    calls: list[list[str]] = []
    start = True
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if _is_punct(tok):
            start = True
            i += 1
            continue
        if start and "=" in tok and tok.split("=", 1)[0].isidentifier():
            i += 1
            continue   # an assignment before the command word
        if start and os.path.basename(tok) == "swarm":
            j = i + 1
            while j < len(tokens) and not _is_punct(tokens[j]):
                j += 1
            argv = tokens[i + 1:j]
            if any(c in "$`" for a in argv for c in a):
                argv = []   # a substitution the shell expands: not what the CLI will see
            if argv:
                calls.append(argv)
                if j < len(tokens) and tokens[j][0] in "<>" and argv[-1].isdigit() and len(argv) > 1:
                    calls.append(argv[:-1])
            i = j
            start = False
            continue
        start = False
        i += 1
    return calls


def _wanted(argv: list[str]) -> bool:
    """Whether argv is a sub-command that asks for its caller (skips a leading --config VALUE)."""
    rest = list(argv)
    while rest and rest[0].startswith("--config"):
        rest = rest[1:] if "=" in rest[0] else rest[2:]
    return bool(rest) and rest[0] in GRANTED


def grant_name(board_key: str, argv: list[str]) -> str:
    d = hashlib.sha256("\0".join([board_key, *argv]).encode("utf-8", "surrogatepass")).hexdigest()
    return f"grant-{d}.json"


def _parse(data: bytes | None) -> dict | None:
    try:
        g = json.loads(data) if data is not None else None
    except (ValueError, RecursionError):
        return None
    if (not isinstance(g, dict) or g.get("v") != GRANT_FORMAT
            or set(g) != {"v", "board_key", "argv", "session_id", "agent_id", "expires_at"}
            or not isinstance(g["session_id"], str) or not g["session_id"]
            or not (g["agent_id"] is None or isinstance(g["agent_id"], str))
            or not isinstance(g["argv"], list) or not all(isinstance(a, str) for a in g["argv"])
            or isinstance(g["expires_at"], bool) or not isinstance(g["expires_at"], (int, float))):
        return None
    return g


def _prune(d: int, now: float) -> None:
    """Remove grants that expired (never taken: the command failed before it asked). Never raises."""
    try:
        for name in compat.listdir(d):
            if name.startswith("grant-") and name.endswith(".json"):
                g = _parse(safefs.read(d, name, GRANT_MAX))
                if g is None or g["expires_at"] <= now:
                    safefs.unlink(d, name)
    except OSError:
        pass


def write_grants(board_key: str, payload: dict, command: str, now: float | None = None) -> int:
    """PreToolUse: one grant per direct `swarm` invocation of a control sub-command in `command`, naming the
    payload's session and (for a subagent) its agent id. Returns how many were written. Raises OSError when the
    enrolled directory is unsafe (the hook logs it; the CLI then finds no grant and refuses)."""
    sid = payload.get("session_id")
    if not isinstance(sid, str) or not sid or len(sid) > enrolment.MAX_NAME or "swarm" not in (command or ""):
        return 0
    agent = payload.get("agent_id")
    agent = agent if isinstance(agent, str) and agent else None
    calls = [a for a in swarm_calls(command) if _wanted(a)]
    if not calls:
        return 0
    now = time.time() if now is None else now
    with enrolment._dir(create=True) as d:
        _prune(d, now)
        for argv in calls:
            body = json.dumps({"v": GRANT_FORMAT, "board_key": board_key, "argv": argv, "session_id": sid,
                               "agent_id": agent, "expires_at": now + GRANT_SECONDS}, sort_keys=True)
            safefs.write_atomic(d, grant_name(board_key, argv), body)
    return len(calls)


# --- the CLI side: who is calling ----------------------------------------------------------------------

_TAKEN: dict = {}


def forget() -> None:
    """Start a new command: the next caller() takes a grant again (drive_cli calls it per handler)."""
    _TAKEN.clear()


def cli_argv() -> list[str]:
    return list(sys.argv[1:])


def caller(board_key: str, argv: list[str] | None = None, now: float | None = None) -> Caller | None:
    """The caller the hook attested for this exact command line, or None. The grant is used up on the first
    call (read, then unlinked; whoever loses the unlink race gets None); later calls in the same command
    return the same answer."""
    argv = cli_argv() if argv is None else list(argv)
    key = (board_key, tuple(argv))
    if key in _TAKEN:
        return _TAKEN[key]
    _TAKEN[key] = None
    now = time.time() if now is None else now
    name = grant_name(board_key, argv)
    try:
        with enrolment._dir(create=False) as d:
            g = _parse(safefs.read(d, name, GRANT_MAX))
            if g is None:
                return None
            compat.unlink(safefs._entry(name), dir_fd=d)   # one use: a lost race raises FileNotFoundError
    except OSError:
        return None
    if g["board_key"] != board_key or g["argv"] != argv or not now < g["expires_at"]:
        return None
    _TAKEN[key] = Caller(g["session_id"], g["agent_id"])
    return _TAKEN[key]


def _ancestor_host() -> str | None:
    try:
        return hosts.nearest_ancestor_host(hosts._proc_parent, os.getppid())
    except Exception:
        return "unknown"   # cannot tell: treat as inside an agent host (fails closed)


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

def requester_of(board_key: str, job: str, who: Caller | None) -> bool:
    """Whether the attested caller is the job's requester main session."""
    if who is None or who.agent_id is not None:
        return False
    owner = enrolment.find_job_owner(board_key, job)
    return owner is not None and bool(owner.session_id) and owner.session_id == who.session_id


def require(action: str, job: str, *, board_key: str, env=None, confirm=None, argv=None, ancestor=None,
            who=False) -> str:
    """Return who may act: "requester" (the job's requester main session, by its grant) or "tty" (a person at
    a terminal outside any agent host, confirmed on /dev/tty). PersonRefused otherwise. `confirm(action, job)`
    replaces the tty prompt and `ancestor()` the process-tree walk (tests); `who` passes an already taken
    caller (False: take it here)."""
    env = os.environ if env is None else env
    if who is False:
        who = caller(board_key, argv)
    if who is not None:
        if requester_of(board_key, job, who):
            return "requester"
        whom = "a subagent" if who.agent_id else "this session"
        raise PersonRefused(f"refused: {action} is a person's action, and {whom} is not the requester session "
                            f"of {job}. Ask the job's lead (--to @EL) or a person.")
    sid = session_of(env)
    if sid and sid in enrolment.agent_sessions(board_key):
        raise PersonRefused(f"refused: {action} is a person's action; this is an enrolled agent session, "
                            "and enrolled sessions never run it. Ask the job's lead (--to @EL) or a person.")
    inside = agent_host(env, ancestor)
    if inside:
        raise PersonRefused(f"refused: {action} needs a person, and this process runs inside an agent host "
                            f"({inside}) with no hook grant for this command. The requester session runs it as "
                            f"a plain `swarm ...` command (its hook attests it); a person runs it from a "
                            "terminal outside any agent.")
    if (confirm or _tty_confirm)(action, job):
        return "tty"
    raise PersonRefused(f"refused: {action} needs a person: run it from the session that activated {job}, "
                        "or from a terminal and confirm on the tty.")
