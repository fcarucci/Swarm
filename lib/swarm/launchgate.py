"""The launch sandbox: hook policy for sessions started by `swarm run` (a launch).

Launch queues are trusted at the command boundary; roles are prompt instructions.
The hook retains credential, production, push and landing protections.
A launch's role comes from its board launch row.
"""
from __future__ import annotations

import contextlib
import dataclasses
import os

from swarm import shellguard

DEFAULT_TARGETS = ("main", "master")   # the branches no launch may push to
HOOK_INIT_TIMEOUT = 4.0

SPAWN_REFUSAL = ("a spawn by {who} is refused: launches are staffed by the lead's `swarm run` (the "
                 "supervisor launches and budgets them), never by subagents")

LEAD_ROLE = "engineering_lead"
JUDGE_ROLES = ("judge", "goal_judge", "ruling_judge")


@dataclasses.dataclass
class Ctx:
    """Who is calling: a supervisor-launched session (or a subagent of one) of `job`. `launch` is the launch's
    name, from its run file."""
    job: str
    launch: str | None = None
    agent_key: str | None = None


@dataclasses.dataclass
class Policy:
    read_only: bool
    judge: bool
    lead: bool
    known: bool = True        # False: the launch's role could not be verified (the strictest rules apply)


def _resume_lookup(cfg: dict, sid: str):
    from swarm import hooks
    return hooks._resume_binding(cfg, sid)


def launch_context(cfg: dict, board_key: str, payload: dict, resume=None, resume_lookup=None):
    """The Ctx of a launch session, or of a subagent of one, from the resume marker bound to the payload's
    session and its run file. None when the session is no launch (no marker, or a run file that names no
    launch)."""
    from swarm.supervisor import runner
    sid = payload.get("session_id")
    m = resume if resume is not None else (resume_lookup or _resume_lookup)(cfg, sid)
    if not (isinstance(m, dict) and isinstance(m.get("resume"), dict) and isinstance(m.get("job"), str)):
        return None
    job = m["job"]
    ctx = Ctx(job=job, agent_key=m["resume"].get("agent_key") or sid)
    rid = m["resume"].get("restart_id")
    run = runner._read_run(f"r{rid}.json", board_key) if isinstance(rid, int) else None
    if (isinstance(run, dict) and runner.run_problem(cfg, run, rid) is None and run.get("job") == job
            and isinstance(run.get("launch_id"), str) and sid in (run.get("session_id"), run.get("agent_key"))
            and isinstance(run.get("launch"), str) and run["launch"]):
        ctx.launch = run["launch"]
    return ctx if ctx.launch else None   # a resumed session whose run file names no launch is no launch


def protected_branches(board_key: str, ctx: Ctx) -> set[str]:
    """The branch names no launch may push to: what the owner record names (target_branch / target_branches),
    and, for the coding recipe (the default when the record says nothing), main and master. The artifact
    recipe has no target."""
    from swarm import enrolment
    out: set[str] = set()
    details = None
    try:
        owner = enrolment.find_job_owner(board_key, ctx.job)
        details = getattr(owner, "owner", None) if owner is not None else None
    except Exception:
        details = None
    details = details if isinstance(details, dict) else {}
    for key in ("target_branch", "target_branches", "target"):
        v = details.get(key)
        out.update([v] if isinstance(v, str) else [x for x in v or () if isinstance(x, str)])
    if details.get("recipe") != "artifact":
        out.update(DEFAULT_TARGETS)
    return {t.removeprefix("refs/heads/") for t in out if t}


def head_branch(directory: str | None) -> str | None:
    """The branch checked out in `directory` (from .git/HEAD, no git run; worktrees and `gitdir:` files
    followed), None when it is detached, not a checkout or unreadable."""
    try:
        d = os.path.abspath(directory or os.getcwd())
        while True:
            dot = os.path.join(d, ".git")
            if os.path.isdir(dot):
                gitdir = dot
                break
            if os.path.isfile(dot):
                with open(dot, encoding="utf-8", errors="replace") as fh:
                    first = fh.readline().strip()
                if first.startswith("gitdir:"):
                    gitdir = os.path.normpath(os.path.join(d, first[len("gitdir:"):].strip()))
                    break
                return None
            parent = os.path.dirname(d)
            if parent == d:
                return None
            d = parent
        with open(os.path.join(gitdir, "HEAD"), encoding="utf-8", errors="replace") as fh:
            head = fh.readline().strip()
        return head[len("ref: refs/heads/"):] if head.startswith("ref: refs/heads/") else None
    except OSError:
        return None


# --- the role of a launch -----------------------------------------------------------------------------

def _open_board(cfg: dict):
    from swarm.board import open_board
    return open_board(cfg, init_timeout=HOOK_INIT_TIMEOUT)


def _launch_role(board, ctx: Ctx) -> str | None:
    """The role of the launch row ctx names: the newest row of that name."""
    rows = [r for r in board.launches(ctx.job) if r.name == ctx.launch]
    return rows[-1].role if rows else None


def launch_policy(cfg: dict, board_key: str, ctx: Ctx, board=None) -> Policy:
    """Roles guide prompts. All launched agents have the same tool permissions."""
    if ctx.launch is None:
        return Policy(read_only=False, judge=False, lead=False, known=False)
    own = board is None
    try:
        board = board or _open_board(cfg)
        role = _launch_role(board, ctx)
    except Exception:
        role = None
    finally:
        if own and board is not None:
            with contextlib.suppress(Exception):
                board.close()
    return Policy(read_only=False, judge=isinstance(role, str) and role.lower() in JUDGE_ROLES,
                  lead=role == LEAD_ROLE, known=role is not None)


# --- PreToolUse -------------------------------------------------------------------------------------

def _command_of(host, payload: dict) -> str:
    command = host.shell_command(payload) or ""
    if not command and payload.get("tool_name") == "PowerShell":   # the same rules as Bash
        command = str((payload.get("tool_input") or {}).get("command") or "")
    return command


def _write_call(host, payload: dict, command: str) -> str | None:
    """Why this call writes (a reason), or None: a file-writing tool, Monitor, an MCP tool that is not on the
    read allowlist, or a shell command that writes."""
    tool = payload.get("tool_name")
    if host.tool_matches(tool, host.write_tools):
        return f"{tool} writes files"
    if tool == "Monitor":
        return "Monitor runs commands outside the launch's limits"
    if isinstance(tool, str) and tool.startswith("mcp__") and not shellguard.mcp_read_allowed(tool):
        return f"the MCP tool {tool} is not on the read allowlist (get, list, search, read, ...)"
    if command:
        why = shellguard.readonly_write_reason(command)
        if why is None and payload.get("tool_name") == "PowerShell":
            why = shellguard.powershell_writes(command)
        return why
    return None


_UNSET = object()


def classify(cfg: dict, payload: dict, resume=None, resume_lookup=None):
    """The Ctx of a launch (or a subagent of one), None for every other session. Raises only on a bug:
    callers treat that as "unknown"."""
    sid = payload.get("session_id")
    if not isinstance(sid, str) or not sid:
        return None
    from swarm.board.autoinit import store_key
    return launch_context(cfg, store_key(cfg), payload, resume, resume_lookup)


def pre_tool(cfg: dict, payload: dict, host, *, resume=None, resume_lookup=None, ctx=_UNSET) -> str | None:
    """Why this tool call of a launch is refused (the reason shown to the agent), None when it may go ahead.
    Runs before everything else a hook does, on every call. An error in the decision of a known launch
    refuses the call (retry); in anything else it lets it through. `ctx` is classify()'s answer when the
    caller has it."""
    tool, sid = payload.get("tool_name"), payload.get("session_id")
    if not isinstance(tool, str) or not isinstance(sid, str) or not sid:
        return None
    known = False
    try:
        if ctx is _UNSET:
            ctx = classify(cfg, payload, resume, resume_lookup)
        if ctx is None:
            return None
        from swarm.board.autoinit import store_key
        bk = store_key(cfg)
        spawn = host.is_spawn(payload)
        known = True
        cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else None
        if host.tool_matches(tool, host.write_tools):
            why = shellguard.protected_write_refusal(payload.get("tool_input"), cwd)
            if why:
                return why
        command = _command_of(host, payload)
        if command:
            why = shellguard.launch_command_refusal(command, lambda: protected_branches(bk, ctx),
                                                   branch_of=_branch_of(payload, command),
                                                   cwd=cwd)
            if why:
                return why
        wants_verdict = bool(command) and shellguard.swarm_verdict_call(command)
        write = _write_call(host, payload, command)
        if not (spawn or wants_verdict or write):
            return None
        policy = launch_policy(cfg, bk, ctx)
        if spawn and (policy.lead or policy.read_only):
            who = "the lead launch" if policy.lead else "a read-only launch (judge or reviewer)"
            if not policy.known:
                who = "a launch whose role could not be verified"
            return SPAWN_REFUSAL.format(who=who)
        if wants_verdict and not policy.judge:
            return ("only the judge launch records a verdict: `swarm verdict` is refused for every other launch "
                    "(say what you found on the board, `swarm post --to @judge`)")
        if write and policy.read_only:
            if not policy.known:
                return (f"{write}: this launch's role could not be verified, so it may not write (retry in a "
                        "moment; the supervisor re-authorizes a launch at each state change)")
            return f"a read-only launch (judge or reviewer) may not write: {write}"
        return None
    except Exception as exc:
        from swarm.jobmarkers import _log_line
        _log_line("launch gate", str(payload.get("agent_id") or sid), f"{type(exc).__name__}: {exc}")
        return ("the launch gate could not decide this call; retry it (a launch fails closed)") if known else None


def _branch_of(payload: dict, command: str):
    """`branch_of(directory)` for a push that names no branch: the branch checked out in `directory` (git -C)
    or in the payload's cwd. A command that moves (cd, pushd) leaves it unknown: such a push is refused."""
    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else None

    def branch_of(directory=None):
        if any(prog in ("cd", "pushd") for prog, _a, _w in shellguard.iter_commands(command)):
            return None
        base = directory if directory and os.path.isabs(directory) else (
            os.path.join(cwd or os.getcwd(), directory) if directory else cwd)
        return head_branch(base)
    return branch_of
