"""Hooks of drive jobs (spec 3.4, 3.11, 4.0, 4.5): the unsandboxed PreToolUse and PostToolUse side of
autonomy. Receipts for the control records the driver acts on, the refusals that keep read-only seats
read-only and every seat off the target branch, and the kick that wakes the session drivers sooner.
Nothing here decides an outcome: the driver checks receipts and verifies what it relies on.

Who is calling comes from host-private state only, never from a board row (an untrusted writer owns those):
the resume marker bound to the session names the restart, the supervisor's run file for that restart names
the launch, and the ledger's intent for that launch names the seat and the action. A seat's role is the one
its `seat_request` receipt carries (drive_authz.receipted_role); a seat that cannot be resolved is treated
as the strictest case, never as the most permissive. The requester's authority belongs to its main session
(a payload with no `agent_id`) and to nothing that shares its session id (its subagents).

A receipt binds the exact submitted argv (every flag the CLI parsed, defaults included) and the decision
fields of the record, and is written only when the record on the board agrees with what was submitted, field
by field; a disagreement writes nothing and is logged (receipt-failures.log, which doctor reports)."""
from __future__ import annotations

import argparse
import contextlib
import dataclasses
import io
import json
import os
import re
import time
from types import SimpleNamespace

from swarm import driveroles, person, receipts, shellguard

RECEIPT_FAILURES = "receipt-failures.log"
RECEIPT_LOG_MAX = 256 * 1024     # the failure log is cut back to its newest RECEIPT_LOG_KEEP bytes past this
RECEIPT_LOG_KEEP = 64 * 1024
RECEIPT_HOURS = 24
DEFAULT_TARGETS = ("main", "master")   # the coding recipe's targets when the owner record names none
RECORD_LINE = re.compile(r"swarm-record: kind=([a-z_]+) id=(\d+) rev=(\d+)")
PERSON_SUBS = ("halt", "unhalt", "deactivate", "activate")
HOOK_INIT_TIMEOUT = 4.0

SPAWN_REFUSAL = ("a spawn by {who} is refused: seats are staffed by the lead's `swarm seat request` (the "
                 "supervisor launches and budgets them), never by subagents")


# --- kick and the failure log ---------------------------------------------------------------------

def kick(now=time.time, cfg: dict | None = None) -> bool:
    """Wake the session drivers (spec 3.11 [T21]): touch the wake file of `cfg`'s board, or every wake file of
    this host when no config is given. No systemctl, no unit, no debounce (a touch is cheap and a swallowed
    kick would be a missed wake); nothing depends on it: the drivers also wake on board changes and deadlines.
    True when a wake file was touched; never raises. (`now` is kept for older callers of the kick.)"""
    from swarm.supervisor import wake
    try:
        if cfg is not None:
            return wake.touch_cfg(cfg)
        return wake.touch_all()
    except Exception:
        return False


def log_receipt_failure(text: str) -> None:
    """A line in the host dir's receipt-failures.log (doctor FAILs on a recent one); one printable line, the
    file cut back to its tail when it grows past RECEIPT_LOG_MAX. Never raises."""
    from swarm import paths, safefs
    try:
        with safefs.dir_fd(paths.host_dir(), create=True, strict_mode=0o700) as d:
            tail = safefs.read_tail(d, RECEIPT_FAILURES, RECEIPT_LOG_MAX + 1)
            if tail is not None and len(tail[0]) > RECEIPT_LOG_MAX:
                keep = tail[0][-RECEIPT_LOG_KEEP:]
                keep = keep[keep.find(b"\n") + 1:]
                safefs.write_atomic(d, RECEIPT_FAILURES, keep)
            safefs.append(d, RECEIPT_FAILURES, safefs.log_safe(f"{time.time():.0f} {text}") + "\n")
    except Exception:
        pass


# --- the host-private state the decisions read ---------------------------------------------------

_LEDGER_CACHE: dict = {}


def has_ledgers() -> bool:
    """Whether this host has any drive ledger at all: the cheap check in front of everything else (a host
    that has never activated a drive job pays one directory open). An unreadable directory counts as yes."""
    from swarm import compat
    from swarm.supervisor import ledger, privfs
    try:
        with privfs.dir_fd(ledger.SUBDIR, create=False) as d:
            return any(n.endswith(".json") for n in compat.listdir(d))
    except FileNotFoundError:
        return False
    except Exception:
        return True


def load_ledger(board_key: str, job: str):
    """The job's ledger or None (not a drive job). A ledger that exists but cannot be read raises
    (ledger.LedgerBadError): the callers treat the job as a drive job they cannot vouch for. Cached per
    process by the file's identity, size and modification time, so a changed ledger is read again."""
    from swarm import compat
    from swarm.supervisor import ledger, privfs
    name = ledger._name(board_key, job)
    try:
        with privfs.dir_fd(ledger.SUBDIR, create=False) as d:
            st = compat.stat(name, dir_fd=d, follow_symlinks=False)
    except FileNotFoundError:
        _LEDGER_CACHE.pop((board_key, job), None)
        return None
    except Exception:
        st = None
    sig = (st.st_ino, st.st_size, st.st_mtime_ns) if st is not None else None
    hit = _LEDGER_CACHE.get((board_key, job))
    if sig is not None and hit is not None and hit[0] == sig:
        return hit[1]
    led = ledger.load(board_key, job)
    if led is None:
        _LEDGER_CACHE.pop((board_key, job), None)
        return None
    if sig is not None:
        if len(_LEDGER_CACHE) > 64:
            _LEDGER_CACHE.clear()
        _LEDGER_CACHE[(board_key, job)] = (sig, led)
    return led


@dataclasses.dataclass
class Ctx:
    """Who is calling. `kind` is "seat" (a supervisor-launched session, or a subagent of one), "requester" (the
    job's requester main session) or "enrolled" (a subagent that joined a drive job by its tag). `seat` and
    `intent` are the ledger's: None when the session could not be resolved to a launch."""
    kind: str
    job: str
    led: object
    seat: str | None = None
    intent: object = None
    agent_key: str | None = None


@dataclasses.dataclass
class Policy:
    read_only: bool
    judge: bool
    lead: bool
    known: bool = True        # False: the seat's role could not be verified (the strictest rules apply)


def _resume_lookup(cfg: dict, sid: str):
    from swarm import hooks
    return hooks._resume_binding(cfg, sid)


def seat_context(cfg: dict, board_key: str, payload: dict, resume=None, resume_lookup=None):
    """The Ctx of a seat session, or of a subagent of one, from the resume marker bound to the payload's
    session, its run file and the ledger. None when the session is no drive seat (no marker and no
    enrolment in a drive job, or the job has no ledger). A seat whose launch cannot be read from the run
    file and the ledger (or whose marker was removed: the enrolment record still names its job) is still
    a seat of its job (kind "seat", seat None): it is held to the strictest rules, never to none."""
    from swarm import enrolment
    from swarm.supervisor import ledger, runner
    sid = payload.get("session_id")
    m = resume if resume is not None else (resume_lookup or _resume_lookup)(cfg, sid)
    marker = isinstance(m, dict) and isinstance(m.get("resume"), dict) and isinstance(m.get("job"), str)
    if marker:
        job, agent_key = m["job"], m["resume"].get("agent_key") or sid
    else:
        rec = enrolment.find(board_key, sid)
        if rec is None:
            return None
        job, agent_key = rec.job, sid
    try:
        led = load_ledger(board_key, job)
    except (ledger.LedgerBadError, OSError):
        led = False          # a drive job whose ledger cannot be read
    if led is None:
        return None
    ctx = Ctx(kind="seat", job=job, led=led or None, agent_key=agent_key)
    if not led or not marker:
        return ctx
    rid = m["resume"].get("restart_id")
    run = runner._read_run(f"r{rid}.json", board_key) if isinstance(rid, int) else None
    if (isinstance(run, dict) and runner.run_problem(cfg, run, rid) is None and run.get("job") == job
            and isinstance(run.get("launch_id"), str) and sid in (run.get("session_id"), run.get("agent_key"))):
        intent = next((i for i in led.intents if i.launch_id == run["launch_id"]), None)
        if intent is not None and intent.seat:
            ctx.intent, ctx.seat = intent, intent.seat
    return ctx


def requester_context(board_key: str, payload: dict, job: str):
    """The Ctx of the job's requester MAIN session (no `agent_id` in the payload: its subagents share the
    session id and are not the requester), or None."""
    sid = payload.get("session_id")
    if not sid or payload.get("agent_id") or not isinstance(job, str):
        return None
    try:
        led = load_ledger(board_key, job)
    except Exception:
        return None
    if led is None or led.requester_session != sid:
        return None
    return Ctx(kind="requester", job=job, led=led)


def requester_job(board_key: str, sid) -> str | None:
    """The first unfinished drive job of this board whose requester is the session `sid` (a main session:
    the caller checks that the payload has no agent_id), or None. Never raises."""
    if not sid:
        return None
    try:
        from swarm.supervisor import ledger
        return next((l.job for l in ledger.unfinished(board_key)      # unfinished(): no terminal ledger
                     if hasattr(l, "requester_session") and l.requester_session == sid), None)
    except Exception:
        return None


def requester_of_unfinished(board_key: str, sid) -> bool:
    """Whether `sid` is the requester session of an unfinished drive job of this board (never raises)."""
    return requester_job(board_key, sid) is not None


def enrolled_context(board_key: str, payload: dict):
    """The Ctx of a subagent that joined a drive job by its tag (enrolment record, ledger): guarded like
    any seat, with no role it could be relied on for."""
    from swarm import enrolment
    agent = payload.get("agent_id")
    if not agent:
        return None
    rec = enrolment.find(board_key, agent)
    if rec is None:
        return None
    try:
        led = load_ledger(board_key, rec.job)
    except Exception:
        return Ctx(kind="enrolled", job=rec.job, led=None, agent_key=agent)
    return Ctx(kind="enrolled", job=rec.job, led=led, agent_key=agent) if led is not None else None


def drive_targets(board_key: str, ctx: Ctx) -> set[str]:
    """The branch names no seat may push to: what the owner record names (target_branch / target_branches),
    the branches the ledger has integrated into, and, for the coding recipe (the default when the record says
    nothing), main and master. The artifact recipe has no target."""
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
    for entry in (getattr(ctx.led, "integrated", None) or {}).values():
        into = entry.get("into") if isinstance(entry, dict) else None
        if isinstance(into, str) and into:
            out.add(into)
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


# --- the role of a seat -----------------------------------------------------------------------------

def _open_board(cfg: dict):
    from swarm.board import open_board
    return open_board(cfg, init_timeout=HOOK_INIT_TIMEOUT)


def seat_policy(cfg: dict, board_key: str, ctx: Ctx, board=None) -> Policy:
    """What a seat may do, from host-private state. A driver-named judge or reviewer, or a session running a
    judge or review action, is read-only without a board lookup; any other seat's role is the one its
    seat_request receipt carries (a board row's role is never taken on its own). Unresolvable means the
    strictest rules (read-only, not a judge, not a lead, known=False)."""
    strict = Policy(read_only=True, judge=False, lead=False, known=False)
    if ctx.seat is None or ctx.led is None:
        return strict
    kind = driveroles.action_kind(getattr(ctx.intent, "action_id", None))
    judge = driveroles.seat_name_is_judge(ctx.seat) or kind in driveroles.JUDGE_KINDS
    if judge or driveroles.seat_name_is_read_only(ctx.seat) or kind in driveroles.REVIEW_KINDS:
        return Policy(read_only=True, judge=judge, lead=False)
    from swarm.supervisor import drive_authz
    own = board is None
    try:
        board = board or _open_board(cfg)
        row = board.seat(ctx.job, ctx.led.generation, ctx.seat)
        role = drive_authz.receipted_role(board_key, ctx.led, row) if row is not None else None
    except Exception:
        return strict
    finally:
        if own and board is not None:
            with contextlib.suppress(Exception):
                board.close()
    if role is None:
        return strict
    return Policy(read_only=driveroles.is_read_only_role(role), judge=driveroles.is_judge_role(role),
                  lead=role == driveroles.LEAD_ROLE and driveroles.seat_name_is_lead(ctx.seat))


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
        return "Monitor runs commands outside the seat's limits"
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
    """The Ctx of a session that belongs to a drive job: a seat (or a subagent of one), a subagent enrolled in
    the job by its tag, or the job's requester main session. None for every other session, and for any host
    with no drive ledger (one directory check). Raises only on a bug: callers treat that as "unknown"."""
    sid = payload.get("session_id")
    if not isinstance(sid, str) or not sid or not has_ledgers():
        return None
    from swarm.board.autoinit import store_key
    bk = store_key(cfg)
    if not payload.get("agent_id"):    # the requester is known from its host-private ledger, before any marker
        job = requester_job(bk, sid)
        if job:
            return Ctx(kind="requester", job=job, led=None)
    ctx = seat_context(cfg, bk, payload, resume, resume_lookup)
    if ctx is not None:
        return ctx
    if payload.get("agent_id"):
        return enrolled_context(bk, payload)
    return None


def pre_tool(cfg: dict, payload: dict, host, *, resume=None, resume_lookup=None, ctx=_UNSET) -> str | None:
    """Why this tool call of a drive session is refused (the reason shown to the agent), None when it may
    go ahead. Runs before everything else a hook does, on every call: the join, the first call of a resumed
    session and a seat whose job is not one of the session's bound markers are gated like any other. An
    error in the decision of a known drive seat refuses the call (retry); in anything else it lets it
    through. Cheap first: no ledger on the host, no work. `ctx` is classify()'s answer when the caller has it."""
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
        if ctx.kind == "requester":
            return SPAWN_REFUSAL.format(who="the requester session") if spawn else None
        known = True
        cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else None
        if host.tool_matches(tool, host.write_tools):
            why = shellguard.protected_write_refusal(payload.get("tool_input"), cwd)
            if why:
                return why
        command = _command_of(host, payload)
        if command:
            why = shellguard.drive_command_refusal(command, lambda: drive_targets(bk, ctx),
                                                   branch_of=_branch_of(payload, command),
                                                   cwd=cwd)
            if why:
                return why
        wants_verdict = bool(command) and shellguard.swarm_verdict_call(command)
        write = _write_call(host, payload, command)
        if not (spawn or wants_verdict or write):
            return None
        policy = seat_policy(cfg, bk, ctx) if ctx.kind == "seat" else Policy(True, False, False, known=False)
        if spawn and (policy.lead or policy.read_only):
            who = "the lead seat" if policy.lead else "a read-only seat (judge or reviewer)"
            if not policy.known:
                who = "a seat whose role could not be verified"
            return SPAWN_REFUSAL.format(who=who)
        if wants_verdict and not policy.judge:
            return ("only the judge seat records a verdict: `swarm verdict` is refused for every other seat "
                    "(say what you found on the board, `swarm post --to @judge`)")
        if write and policy.read_only:
            if not policy.known:
                return (f"{write}: this seat's role could not be verified, so it may not write (retry in a "
                        "moment; the supervisor re-authorizes a seat at each state change)")
            return f"a read-only seat (judge or reviewer) may not write: {write}"
        return None
    except Exception as exc:
        from swarm.jobmarkers import _log_line
        _log_line("drive gate", str(payload.get("agent_id") or sid), f"{type(exc).__name__}: {exc}")
        return ("the drive gate could not decide this call; retry it (a seat fails closed)") if known else None


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


# --- PostToolUse: receipts ---------------------------------------------------------------------------

def output_text(payload: dict) -> str:
    """The text a Bash call printed, from the shape the host reports: Claude's `tool_response` object
    (`stdout`), Codex's plain string (the command's output), or a list of text parts. Never a field the
    host does not send (there is no exit_code in a Claude payload; success is the CLI's record line)."""
    resp = payload.get("tool_response")
    if isinstance(resp, str):
        return resp
    if isinstance(resp, dict):
        if resp.get("interrupted") is True or (isinstance(resp.get("exit_code"), int) and resp["exit_code"] != 0):
            return ""
        for key in ("stdout", "output", "text"):
            if isinstance(resp.get(key), str):
                return resp[key]
        return ""
    if isinstance(resp, list):
        return "\n".join(p if isinstance(p, str) else str(p.get("text", "")) for p in resp
                         if isinstance(p, (str, dict)))
    return ""


def record_of(payload: dict):
    """(kind, id, rev) from the one record format on the output's last non-empty line, or None: the CLI printed
    it as its last line, so a command that failed (or that was interrupted) has none."""
    lines = [l for l in output_text(payload).splitlines() if l.strip()]
    m = RECORD_LINE.fullmatch(lines[-1].strip()) if lines else None
    return (m.group(1), int(m.group(2)), int(m.group(3))) if m else None


_SEAT_RECORD_SUBS = ("request", "ack", "release", "cancel")
_RECORD_SUBS = ("done", "review", "verdict", "post", "ask", "answer", "goal-ready", "halt", "unhalt",
                "deactivate", "activate", "land")


def _sub_of(args: list[str]) -> tuple[str | None, list[str]]:
    """(subcommand, the argv from it) of what follows `swarm`, swarm's own options skipped."""
    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 1 if "=" in args[i] else 2
    return (args[i], args[i:]) if i < len(args) else (None, [])


def record_calls(command: str) -> list[list[str]]:
    """The argv (from the subcommand) of every `swarm` call in `command` that can create or change a control
    record. A command with exactly one is receipted; with several, none (the last line names only one)."""
    out = []
    for prog, args, _wrappers in shellguard.iter_commands(command):
        if prog != "swarm":
            continue
        sub, argv = _sub_of(args)
        if sub in _RECORD_SUBS or (sub == "seat" and argv[1:2] and argv[1] in _SEAT_RECORD_SUBS) or (
                sub == "drive" and argv[1:2] == ["repair"]):   # a Codex repair's CONTROL-REQUEST
            out.append(argv)
    return out


def _fallback_parser() -> argparse.ArgumentParser:
    """The control commands this module reads, as the CLI defines them (the CLI's own parser is used
    first; this one covers a command it does not know in this build)."""
    p = argparse.ArgumentParser(prog="swarm", add_help=False, allow_abbrev=False, exit_on_error=False)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("seat", add_help=False, allow_abbrev=False)
    ss = s.add_subparsers(dest="scmd", required=True)
    rq = ss.add_parser("request", add_help=False, allow_abbrev=False)
    for f in ("job", "seat", "role", "brief"):
        rq.add_argument("--" + f, required=True)
    rq.add_argument("--harness", default="claude", choices=("claude", "codex"))
    for f in ("model", "workdir", "title", "task"):
        rq.add_argument("--" + f)
    rq.add_argument("--complexity", choices=("simple", "standard", "complex"))
    ack = ss.add_parser("ack", add_help=False, allow_abbrev=False)
    ack.add_argument("--job", required=True)
    ack.add_argument("--upto", required=True, type=int)
    rel = ss.add_parser("release", add_help=False, allow_abbrev=False)
    rel.add_argument("--job", required=True)
    rel.add_argument("--seat", required=True)
    rel.add_argument("--withdraw-artifact")
    rel.add_argument("--reason")
    rv = sub.add_parser("review", add_help=False, allow_abbrev=False)
    rv.add_argument("--job", required=True)
    rv.add_argument("--artifact", required=True)
    rv.add_argument("outcome", choices=("verified", "changes"), type=str.lower)
    rg = rv.add_mutually_exclusive_group(required=True)
    rg.add_argument("--report")
    rg.add_argument("--report-text")
    rv.add_argument("--key")
    gr = sub.add_parser("goal-ready", add_help=False, allow_abbrev=False)
    gr.add_argument("--job", required=True)
    gr.add_argument("--candidate", required=True)
    gr.add_argument("--add-requirements", action="append")
    for name in ("halt", "unhalt", "deactivate"):
        h = sub.add_parser(name, add_help=False, allow_abbrev=False)
        h.add_argument("--job", required=True)
        h.add_argument("--reason")
    ask = sub.add_parser("ask", add_help=False, allow_abbrev=False)
    ask.add_argument("--job", required=True)
    ask.add_argument("--key")
    ask.add_argument("--to", required=True)
    ask.add_argument("text", nargs="+")
    for f in ("options", "default", "expires", "blocks"):
        ask.add_argument("--" + f)
    ans = sub.add_parser("answer", add_help=False, allow_abbrev=False)
    ans.add_argument("id")
    ans.add_argument("--key")
    ans.add_argument("text", nargs="*")
    ans.add_argument("--option")
    ans.add_argument("--default", action="store_true")
    ans.add_argument("--comment")
    ans.add_argument("--reopen")
    return p


def parse_swarm(argv: list[str]):
    """The namespace of a swarm argv by the CLI's own parser (so defaults and abbreviations are the CLI's),
    else by this module's copy of the control commands; None when neither parses it."""
    for build in (_cli_parser, _fallback_parser):
        try:
            with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                parser = build()
                if build is _fallback_parser and argv and argv[0] in ("ask", "answer", "review"):
                    # positionals after options (`answer 3 --key K text`): Python 3.11's argparse needs the
                    # intermixed parser for a `*` positional
                    ns = parser._subparsers._group_actions[0].choices[argv[0]].parse_intermixed_args(list(argv[1:]))
                    ns.cmd = argv[0]
                    return ns
                return parser.parse_args(list(argv))
        except (SystemExit, Exception):
            continue
    return None


def _cli_parser():
    """The CLI's own parser WITH its plugins' commands and arguments, exactly as `swarm` builds it. Without
    them a plugin option (e.g. engineering-team's `activate --team`) fails the parse here, no grant is
    written, and the CLI refuses the requester's own control command as "needs a person" (2026-10-15).
    The CLI module's run-global plugin state is restored: a hook process never keeps plugins loaded."""
    from swarm import cli
    parser = cli._parser()
    saved = (cli.PLUGINS, getattr(cli, "PLUGIN_DISCOVERY_ERROR", None))
    try:
        cli._load_plugins([], parser)
    except Exception:   # discovery never fails a parse: without plugins, a core command still parses
        pass
    finally:
        cli.PLUGINS, cli.PLUGIN_DISCOVERY_ERROR = saved
    return parser


_ARGV_SKIP = ("config", "cmd", "scmd", "no_color", "func")


def submitted_argv(ns) -> list[str]:
    """The exact submitted argument vector as the parser read it, defaults included, in a fixed order:
    the subcommand, then every parsed value as `--name value` (flags without a value as `--name`). The
    receipt's args_sha is the hash of this list: a command with other arguments has another hash."""
    out = [str(ns.cmd)] + ([str(ns.scmd)] if getattr(ns, "scmd", None) else [])
    for key in sorted(vars(ns)):
        v = getattr(ns, key)
        if key in _ARGV_SKIP or v is None or v is False or callable(v):
            continue
        flag = "--" + key.replace("_", "-")
        if v is True:
            out.append(flag)
        elif isinstance(v, (list, tuple)):
            for x in v:
                out += [flag, str(x)]
        else:
            out += [flag, str(v)]
    return out


class Refused(Exception):
    """No receipt: `why` says what disagreed; `log` is False for the ordinary cases (not the caller's kind of
    command, not its authority) and True for a record that disagrees with what was submitted."""

    def __init__(self, why: str, log: bool = True):
        super().__init__(why)
        self.why, self.log = why, log


_FIELDS = ("job", "seat", "role", "harness", "model", "brief_path", "workdir", "title", "task_ref", "complexity")


def default_model(cfg: dict, role: str) -> str | None:
    """The model `swarm seat request` picks for `role` when `--model` is not given: the seat command's own
    resolver (None when it cannot be asked: the hook then cannot confirm the row's model and writes no
    receipt)."""
    try:
        from swarm import drive_cli
        return drive_cli._model_for(cfg, role, None)
    except Exception:
        return None


def _message(board, job: str, rid: int):
    return next((m for m in board.recent_messages(300, job) if m.id == rid), None)


def _expect_message(board, job: str, rid: int, text: str, author, to=None, *, any_to=False):
    """The message `rid` of `job` must be exactly what the command posts (text as the board stores it),
    from `author` (None: not checked), to `to`."""
    from swarm.board.base import normalize_message
    m = _message(board, job, rid)
    want = normalize_message(text, board.message_cap())[0]
    if m is None or m.message != want:
        raise Refused(f"message {rid} is not what the command posted")
    if author is not None and m.agent_name != author:
        raise Refused(f"message {rid} was posted by another agent")
    if not any_to and m.to_agent != to:
        raise Refused(f"message {rid} is addressed differently from the command")
    if any_to and m.to_agent is None:
        raise Refused(f"message {rid} is not addressed")
    return m


def _build(cfg, bk, board, ctx, who: Policy | None, ns, rid: int, rev: int, payload: dict, author):
    """(receipt kind, decision fields, submitted argv) of the record `rid` the command printed, checked against
    what was submitted; Refused when it is not the record the command made or its caller may not."""
    led, job = ctx.led, ctx.job
    sub = ns.cmd
    requester = ctx.kind == "requester"
    seat = ctx.kind == "seat"
    lead = bool(who and who.lead)

    def need(ok: bool, what: str):
        if not ok:
            raise Refused(f"{what} is not this caller's command", log=False)

    argv = submitted_argv(ns)
    if sub in PERSON_SUBS:
        need(requester, f"swarm {sub}")
        if sub == "activate":
            _activation_ok(board, ctx, payload)
        return "person", {"action": sub, "target": job}, argv
    if sub == "seat":
        scmd = ns.scmd
        need(requester or lead, f"swarm seat {scmd}")
        if scmd == "request":
            return _seat_request(cfg, board, ctx, ns, rid, rev, payload)
        if scmd == "ack":
            if rid != ns.upto:
                raise Refused("the acknowledged id is not the one submitted")
            return "ack", {"request_id": str(ns.upto)}, argv
        if scmd == "release" and ns.withdraw_artifact:
            row = board.seat(job, led.generation, ns.seat)
            if row is None or row.id != rid:
                raise Refused("the withdrawn seat is not the row the command printed")
            return "withdraw", {"request_id": str(row.id)}, argv
        raise Refused("not a receipted seat command", log=False)
    if sub == "done":
        need(seat and who is not None and who.known and not who.read_only, "swarm done")
        artifact = ns.artifact or (f"{ns.branch}@{ns.sha}" if ns.branch and ns.sha else None)
        if not artifact or not artifact.strip():
            raise Refused("a hand-off in a drive job names its artifact (--artifact REF)")
        text = "DONE " + json.dumps(artifact, ensure_ascii=False) + (" | " + ns.summary if ns.summary else "")
        _expect_message(board, job, rid, text, author)
        group, _, sha = artifact.partition("@")
        return "done", {"artifact": artifact, "sha": sha, "group": group}, argv
    if sub == "review":
        need(seat and who is not None and who.known and who.read_only and not who.judge, "swarm review")
        artifact = ns.artifact.strip()
        outcome = ns.outcome.lower()
        from swarm.drive_cli import review_ref
        _expect_message(board, job, rid, f"REVIEW {artifact} {outcome.upper()} {review_ref(getattr(ns, 'report', None))}",
                        author)
        return "review", {"group": artifact.partition("@")[0], "artifact": artifact, "outcome": outcome}, argv
    if sub == "verdict":
        need(seat and who is not None and who.known and who.judge, "swarm verdict")
        return "verdict", _verdict_decision(board, ctx, ns, rid, author), argv
    if sub == "goal-ready":
        need(requester or lead, "swarm goal-ready")
        from swarm.supervisor import drive_authz
        digest = drive_authz.requirements_digest(led.pinned_requirements or {})
        if not led.pinned_requirements:
            raise Refused("the ledger pins no requirements: the goal-ready is invalid")
        _expect_message(board, job, rid, f"GOAL-READY {ns.candidate} req={digest}", None)
        return "goal_ready", {"generation": led.generation, "candidate": ns.candidate}, argv
    if sub == "land":
        # The integrator seat's `swarm land`: its LAND-REQUEST post is what the driver executes (land.find_requests
        # checks this receipt and that its seat is the integrator seat of the artifact's branch).
        if getattr(ns, "land_cmd", None):
            raise Refused("`land hosts` records nothing", log=False)
        need(seat and who is not None and who.known and not who.read_only and bool(ctx.seat)
             and ctx.seat.startswith("integrator-"), "swarm land")
        from swarm import land
        m = _expect_message(board, job, rid, land.request_text((ns.artifact or "").strip()), author)
        return "post", {"to": m.to_agent, "body": m.message}, argv
    if sub == "post":
        need((seat and who is not None) or requester, "swarm post")
        if not ns.to:
            raise Refused("an unaddressed post wakes no seat", log=False)
        m = _expect_message(board, job, rid, " ".join(ns.message), author, ns.to if not ns.to.startswith("@") else None,
                            any_to=ns.to.startswith("@"))
        return "post", {"to": m.to_agent, "body": m.message}, argv
    if sub == "ask":
        need(seat and who is not None, "swarm ask")
        b = board.blocker(rid)
        if b is None or b.kind != "question" or b.job != job or rev != 1:
            raise Refused(f"question {rid} is not the record the command made")
        from swarm.board.base import normalize_message
        if (b.waiting_on != ns.to or b.reason != " ".join(ns.text).strip() or b.default_value != ns.default
                or (ns.expires is not None and b.until is None)
                or (author is not None and b.created_by != author)):
            raise Refused(f"question {rid} differs from what was submitted")
        from swarm.supervisor import drive_authz
        return "ask", {"question": b.reason, "until": drive_authz.canonical_until(b.until),
                       "default": b.default_value}, argv
    if sub == "answer":
        need((seat and who is not None) or requester, "swarm answer")
        qid = int(str(ns.id).lstrip("Qq"))
        b = board.blocker(qid)
        if b is None or b.kind != "question" or b.job != job or rid != qid:
            raise Refused(f"question {qid} is not the one the command answered")
        if ns.reopen is not None:
            value = ns.reopen
        elif ns.option is not None:
            value = ns.option
        elif ns.default:
            value = b.default_value
        elif ns.text:
            value = " ".join(ns.text)
        else:
            raise Refused("a comment is not an answer", log=False)
        if b.state == "open" or b.resolved_how != value or (author is not None and b.resolved_by != author):
            raise Refused(f"question {qid} was not answered as submitted")
        return "answer", {"ask_id": str(qid), "answer": value}, argv
    raise Refused("not a receipted command", log=False)


def _seat_request(cfg, board, ctx, ns, rid, rev, payload):
    """`seat_request` kind, decision and canonical argv of a `swarm seat request`: every field of the row (the
    ten the command sets, defaults included) must equal what the submitted arguments give."""
    from swarm.supervisor import drive_authz
    led = ctx.led
    row = board.seat(ctx.job, led.generation, ns.seat)
    if row is None or row.id != rid or row.revision != rev:
        raise Refused("the row is not the one the command printed (replaced since)")
    harness, _why = driveroles.effective_harness(ns.role, ns.harness)
    model = ns.model or default_model(cfg, ns.role)
    workdir = os.path.realpath(os.path.expanduser(ns.workdir or payload.get("cwd") or ""))
    want = SimpleNamespace(job=ctx.job, seat=ns.seat, role=ns.role, harness=harness, model=model,
                           brief_path=ns.brief, workdir=workdir, title=ns.title, task_ref=ns.task,
                           complexity=ns.complexity or "standard")
    differ = [f for f in _FIELDS if getattr(row, f) != getattr(want, f)]
    if differ:
        raise Refused(f"the row differs from the submitted arguments in {', '.join(differ)}")
    return ("seat_request", drive_authz.seat_decision(row), drive_authz.canonical_seat_args(want))


def _verdict_decision(board, ctx, ns, rid: int, author) -> dict:
    verdict = ns.verdict
    if verdict == "show":
        raise Refused("`verdict show` records nothing", log=False)
    reason = (getattr(ns, "reason_opt", None) or " ".join(ns.reason or ())).strip()
    scope = getattr(ns, "scope", None) or ("goal" if verdict == "unreachable" else "artifact")
    artifact = ns.artifact if scope == "artifact" else None
    if scope == "artifact" and not artifact:
        raise Refused("an artifact verdict in a drive job names its artifact (--artifact REF)")
    if scope != "artifact" and not ns.candidate:
        raise Refused("a goal or ruling verdict names its candidate (--candidate)")
    from swarm.review import verdict_text   # the one VERDICT format (a goal/ruling verdict names what it judges)
    _expect_message(board, ctx.job, rid, verdict_text(verdict, reason, scope=scope, generation=ctx.led.generation,
                                                      candidate=ns.candidate, req=ns.req), author)
    return {"verdict": verdict, "scope": scope, "generation": ctx.led.generation, "candidate": ns.candidate,
            "req": ns.req, "artifact": artifact}


def _activation_ok(board, ctx, payload: dict) -> None:
    """[RR1a] activation order: the owner record (host-private, written first) names this session, the board
    carries the drive claim, and the ledger (written last) names this session as the requester (the caller's
    requester_context has checked that last one)."""
    from swarm import enrolment
    bk = ctx.led.board_key
    owner = enrolment.find_job_owner(bk, ctx.job)
    if owner is None or owner.session_id != payload.get("session_id"):
        raise Refused("the owner record does not name this session", log=False)
    if (board.job_data(ctx.job) or {}).get("drive.enabled") != "1":
        raise Refused("the board carries no drive claim for the job", log=False)


def post_tool(cfg: dict, payload: dict, host, *, resume=None, resume_lookup=None) -> bool:
    """PostToolUse of a Bash call that ran one control command in a drive job: writes the receipt the
    driver checks (receipts.write), then kicks the session drivers (their wake file). True when a receipt was written. Cheap
    prechecks first (the command mentions swarm and printed a record line), the board only after them.
    Only a seat's own session or the requester's main session (never a subagent: a payload with an
    `agent_id`) gets a receipt, and only for the commands its authority covers. A record that disagrees
    with the submitted arguments, or a failed write, is logged and writes nothing."""
    command = host.shell_command(payload) or ""
    if "swarm" not in command or payload.get("agent_id") or not payload.get("session_id"):
        return False
    rec = record_of(payload)
    if rec is None or not has_ledgers():
        return False
    calls = record_calls(command)
    if len(calls) != 1:
        return False
    ns = parse_swarm(calls[0])
    kind, rid, rev = rec
    if ns is not None and kind == "control_request":
        return control_request_receipt(cfg, payload, ns, rid, rev, resume=resume, resume_lookup=resume_lookup)
    if ns is None or drive_kind(ns) != kind:
        return False
    from swarm.board.autoinit import store_key
    bk = store_key(cfg)
    job_label = getattr(ns, "job", None) or "?"
    try:
        ctx = None
        if not payload.get("agent_id") and getattr(ns, "job", None) and requester_context(bk, payload, ns.job):
            ctx = None          # the job's requester: a planted marker must not make it a seat
        else:
            ctx = seat_context(cfg, bk, payload, resume, resume_lookup)
        board = _open_board(cfg)
        with contextlib.closing(board):
            if ctx is None:
                job = getattr(ns, "job", None)
                if job is None and ns.cmd == "answer":
                    b = board.blocker(int(str(ns.id).lstrip("Qq")))
                    job = b.job if b is not None else None
                ctx = requester_context(bk, payload, job)
            if ctx is None or ctx.led is None or (getattr(ns, "job", None) not in (None, ctx.job)):
                return False
            job_label = ctx.job
            who = None
            author = None
            if ctx.kind == "seat":
                if ctx.seat is None or ctx.intent is None or ctx.intent.state == "concluded":
                    return False
                who = seat_policy(cfg, bk, ctx, board)
                author = board.active_agent_name(ctx.agent_key)
                if author is None:
                    return False
            rkind, decision, argv = _build(cfg, bk, board, ctx, who, ns, rid, rev, payload, author)
            action, attempt, seat = ((ctx.intent.action_id, ctx.intent.attempt, ctx.seat) if ctx.kind == "seat"
                                     else (requester_action(ns), 0, None))
            r = receipts.Receipt(
                board_key=bk, job=ctx.job, generation=ctx.led.generation, incarnation=ctx.led.incarnation,
                action_id=action, attempt=attempt, seat=seat, kind=rkind, record_id=str(rid), rev=rev,
                content_sha=receipts.content_sha(rkind, decision), args_sha=receipts.args_sha(argv),
                expires_at=time.time() + RECEIPT_HOURS * 3600)
    except Refused as why:
        if why.log:
            log_receipt_failure(f"{job_label} {kind} id={rid} rev={rev}: not receipted: {why.why}")
        return False
    except Exception as exc:
        log_receipt_failure(f"{job_label} {kind} id={rid} rev={rev}: {type(exc).__name__}: {exc}")
        return False
    from swarm.supervisor import barrier
    barrier.hit("hook_after_cli_before_receipt")
    try:
        receipts.write(bk, r)
    except Exception as exc:
        log_receipt_failure(f"{r.job} {r.kind} id={rid} rev={rev}: {type(exc).__name__}: {exc}")
        return False
    kick(cfg=cfg)
    return True


def drive_kind(ns) -> str:
    """The record-format kind (swarm-record: kind=...) the parsed command prints."""
    if ns.cmd == "seat":
        if ns.scmd == "ack":
            return "ack"
        return "withdraw" if ns.scmd == "release" and getattr(ns, "withdraw_artifact", None) else "seat"
    return {"goal-ready": "goal_ready", "land": "post"}.get(ns.cmd, ns.cmd)   # a LAND-REQUEST is a post (land.REQUEST_KIND)


def requester_action(ns) -> str:
    """The action id of a receipt the requester session's command earns: `requester:seat` for every seat
    command (drive_authz checks that one for a seat request), else `requester:<command>`."""
    return "requester:seat" if ns.cmd == "seat" else "requester:" + ns.cmd.replace("-", "_")



# --- PreToolUse: grants (who runs a control command, attested before it runs) -------------------------------
#
# A receipt (post_tool) proves to the driver that a session's command made a record. It is written after the
# command, so it cannot tell the CLI who is calling while the command runs: the CLI's own ledger writes
# (halt, unhalt, an ack's mark, a withdrawal, pins, a repair) need that before. A grant is the same kind of
# host-private receipt, written by this PreToolUse hook from the same contexts (requester_context: the
# requester's MAIN session; seat_context: the seat the host-private chain names). It is bound (record id) to
# args_sha of the submitted argv, the config path and the pid of the agent host process (claude/codex) the
# call runs under, so only a process of that session's own tree can match it. It is written only when the
# whole shell call is one swarm invocation that runs (optional `cd DIR &&` before it, pipes, redirections and
# `&& ...` after it), never for a call pre_tool refused; the CLI uses it once (receipts.take) and the
# PostToolUse of the same call removes whatever is left (drop_grants), so it lives no longer than the call
# (GRANT_SECONDS bounds a call the host denied, which has no PostToolUse). A subagent gets none.

GRANT_SECONDS = 60.0
GRANT_SUBS = ("halt", "unhalt", "seat", "goal-ready", "drive", "deactivate", "activate")
_PUNCT = "();<>|&\n"
_REDIRECTS = {">", ">>", "<", ">&", "<&", ">|", "&>", "&>>"}


def grant_id(ns, pid=None) -> str:
    """The record id of the grant for the parsed command `ns` run under the agent host process `pid`
    (person.host_pid(): the hook and the CLI find the same one): args_sha of its submitted argv, its config
    path (`--config`, default included) and that pid."""
    return receipts.args_sha(submitted_argv(ns) + [f"--config={getattr(ns, 'config', None)}", f"host-pid={pid}"])


def _tokens(command: str):
    import shlex
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=_PUNCT)
        lex.whitespace = " \t\r"
        lex.whitespace_split = True
        return list(lex)
    except ValueError:
        return None


def _punct(tok: str) -> bool:
    return bool(tok) and all(c in _PUNCT for c in tok)


def simple_call(command: str):
    """The argv after `swarm` when the whole shell call is ONE swarm invocation that runs: optional `cd DIR &&`
    segments before it, and only redirections, pipes or `&& ...` after it. None for anything else (`;`, `||`,
    a background `&`, a newline, a subshell or function body, an `if`, another command first, a substitution):
    such a call gets no grant, so a grant is never planted for a command that does not run."""
    toks = _tokens(command or "")
    if not toks:
        return None
    i = 0
    while i + 2 < len(toks) and toks[i] == "cd" and not _punct(toks[i + 1]) and toks[i + 2] == "&&":
        i += 3
    if i >= len(toks) or os.path.basename(toks[i]) != "swarm":
        return None
    j = i + 1
    while j < len(toks) and not _punct(toks[j]):
        j += 1
    argv = toks[i + 1:j]
    if not argv or any(c in "$`" for a in argv for c in a):
        return None
    rest = toks[j:]
    if rest and rest[0] in _REDIRECTS and argv[-1].isdigit():
        argv = argv[:-1]                       # `2>&1`: the fd number is the shell's, not an argument
    k = 0
    while k < len(rest):                       # what may follow: redirections, then `|` or `&&` and anything
        t = rest[k]
        if t in _REDIRECTS:
            k += 2
            continue
        if t in ("|", "&&"):
            return argv                        # what follows a pipe or && cannot keep the swarm call from running
        return None
    return argv


def _grant_calls(command: str) -> list:
    argv = simple_call(command)
    if argv is None:
        return []
    sub, argv = _sub_of(argv)
    return [argv] if sub in GRANT_SUBS else []


def _grant_owner(board_key: str, payload: dict, ns):
    """(led or None, action_id, attempt, seat) of the caller this grant attests for `ns`, or None."""
    from swarm import enrolment
    job = getattr(ns, "job", None)
    if not isinstance(job, str):
        return None
    ctx = requester_context(board_key, payload, job)
    if ctx is not None:
        return ctx.led, requester_action(ns), 0, None
    if ns.cmd == "drive" and getattr(ns, "dcmd", None) == "repair":
        # the ledger does not read (that is why it is repaired): the owner record names the requester
        try:
            led = load_ledger(board_key, job)
        except Exception:
            led = None
        owner = enrolment.find_job_owner(board_key, job)
        if led is None and owner is not None and owner.session_id and owner.session_id == payload.get("session_id"):
            return None, "requester:drive_repair", 0, None
    return None


def write_grants(cfg: dict, payload: dict, host, ctx=None, now=None) -> int:
    """Write a grant for each direct `swarm` control command (GRANT_SUBS) of this shell call whose caller is
    the requester's main session of its job, or a seat the host-private chain resolves (`ctx`, classify()'s
    answer). Returns how many were written. Never for a subagent. Raises on an unsafe enrolled directory
    (the caller logs it; the CLI then finds no grant and refuses)."""
    if payload.get("agent_id") or not isinstance(payload.get("session_id"), str):
        return 0
    command = host.shell_command(payload) or ""
    if "swarm" not in command:
        return 0
    calls = _grant_calls(command)
    if not calls:
        return 0
    from swarm.board.autoinit import store_key
    bk = store_key(cfg)
    now = time.time() if now is None else now
    n = 0
    for argv in calls:
        ns = parse_swarm(argv)
        if ns is None:
            continue
        owner = None
        if ctx is not None and ctx.kind == "seat" and ctx.job == getattr(ns, "job", None) and ctx.led \
                and ctx.seat and ctx.intent is not None:
            owner = ctx.led, ctx.intent.action_id, ctx.intent.attempt, ctx.seat
        else:
            owner = _grant_owner(bk, payload, ns)
        if owner is None:
            continue
        led, action, attempt, seat = owner
        rid = grant_id(ns, person.host_pid())
        receipts.write(bk, receipts.Receipt(
            board_key=bk, job=ns.job, generation=led.generation if led else 1,
            incarnation=led.incarnation if led else receipts.GRANT_INCARNATION_NO_LEDGER,
            action_id=action, attempt=attempt, seat=seat, kind="grant", record_id=rid, rev=1,
            content_sha=receipts.content_sha("grant", {"argv_sha": rid}), args_sha=rid,
            expires_at=now + GRANT_SECONDS))
        n += 1
    return n


def write_grant_argv(cfg: dict, argv: list[str], ctx, pid: int | None, now=None) -> str | None:
    """A seat broker's grant (Task 20): the broker, outside the jail, attests that the seat `ctx` (resolved from
    its own launch record, as the hook would) submits exactly the brokered `argv` (leading `--config` included),
    run under the seat's session process `pid`. The grant id, or None when `argv` is no grant command or `ctx`
    is no resolved seat of its job."""
    if not argv or not ctx or getattr(ctx, "kind", None) != "seat" or not ctx.led or not ctx.seat \
            or ctx.intent is None:
        return None
    ns = parse_swarm(list(argv))
    if ns is None or getattr(ns, "cmd", None) not in GRANT_SUBS or ctx.job != getattr(ns, "job", None):
        return None
    from swarm.board.autoinit import store_key
    bk = store_key(cfg)
    now = time.time() if now is None else now
    rid = grant_id(ns, pid)
    led = ctx.led
    receipts.write(bk, receipts.Receipt(
        board_key=bk, job=ns.job, generation=led.generation, incarnation=led.incarnation,
        action_id=ctx.intent.action_id, attempt=ctx.intent.attempt, seat=ctx.seat, kind="grant", record_id=rid,
        rev=1, content_sha=receipts.content_sha("grant", {"argv_sha": rid}), args_sha=rid,
        expires_at=now + GRANT_SECONDS))
    return rid


def drop_grants(cfg: dict, payload: dict, host) -> int:
    """PostToolUse (or a refused PreToolUse): remove every grant this shell call could have been given, used
    or not, so none outlives its call. Returns how many were removed. Never raises."""
    try:
        command = host.shell_command(payload) or ""
        calls = _grant_calls(command) if "swarm" in command else []
        if not calls:
            return 0
        from swarm.board.autoinit import store_key
        bk = store_key(cfg)
        n = 0
        for argv in calls:
            ns = parse_swarm(argv)
            if ns is not None and isinstance(getattr(ns, "job", None), str):
                n += receipts.drop(bk, ns.job, "grant", grant_id(ns, person.host_pid()))
        return n
    except Exception:
        return 0


# --- PostToolUse: control requests (a Codex session's ledger-writing commands, drive_control) -------------

def control_request_receipt(cfg: dict, payload: dict, ns, rid: int, rev: int, *, resume=None,
                            resume_lookup=None) -> bool:
    """The receipt of a CONTROL-REQUEST the command just posted (record kind control_request): the message
    `rid` must be exactly the request the submitted command makes (drive_control.request_text), and the caller
    one this hook can name from host-private state, as for every receipt: the seat the marker, run file and
    ledger intent name, or the job's requester main session (for a repair of a ledger that does not read: the
    owner record's session). Never a subagent (post_tool returned before). The driver decides authority and
    applies the request (drive_control.apply). True when a receipt was written."""
    from swarm import drive_control
    from swarm.board.autoinit import store_key
    kind = drive_control.kind_of(ns)
    job = getattr(ns, "job", None)
    if kind is None or rev != 1 or not isinstance(job, str):
        return False
    bk = store_key(cfg)
    try:
        ctx = seat_context(cfg, bk, payload, resume, resume_lookup)
        owner = None
        if ctx is not None and ctx.kind == "seat" and ctx.job == job and ctx.led and ctx.seat and ctx.intent:
            owner = ctx.led, ctx.intent.action_id, ctx.intent.attempt, ctx.seat
        else:
            owner = _grant_owner(bk, payload, ns)
        if owner is None:
            return False
        led, action, attempt, seat = owner
        if (led is None) != (kind == "repair"):
            return False
        board = _open_board(cfg)
        with contextlib.closing(board):
            m = _message(board, job, rid)
            if m is None or m.message != drive_control.request_text(ns):
                raise Refused(f"message {rid} is not the request the command posts")
        fields = drive_control.fields_of(ns)
        sha = drive_control.args_sha(fields)
        r = receipts.Receipt(
            board_key=bk, job=job, generation=led.generation if led else 1,
            incarnation=led.incarnation if led else receipts.GRANT_INCARNATION_NO_LEDGER,
            action_id=action, attempt=attempt, seat=seat, kind=drive_control.KIND, record_id=str(rid), rev=1,
            content_sha=receipts.content_sha(drive_control.KIND, {"request": kind, "args_sha": sha}),
            args_sha=sha, expires_at=time.time() + RECEIPT_HOURS * 3600)
        drive_control.store_body(bk, job, rid, fields)   # the arguments first: a receipt never lacks them
        receipts.write(bk, r)
    except Refused as why:
        if why.log:
            log_receipt_failure(f"{job} control_request id={rid}: not receipted: {why.why}")
        return False
    except Exception as exc:
        log_receipt_failure(f"{job} control_request id={rid}: {type(exc).__name__}: {exc}")
        return False
    kick(cfg=cfg)
    return True


def is_drive_judge(cfg: dict, board, job: str, payload: dict, resume=None) -> bool:
    """Whether the session of `payload` is the driver's judge seat of `job`: its host-private launch (the
    resume marker and run file name the seat's intent) and the seat row's receipted role say "judge". A
    prompt tag, a board row or a `join --judge` never decide it (QA bug 4). Fails closed."""
    try:
        from swarm.board.autoinit import store_key
        from swarm.supervisor import drive_authz
        bk = store_key(cfg)
        ctx = seat_context(cfg, bk, payload, resume=resume)
        if ctx is None or ctx.led is None or not ctx.seat or ctx.job != job:
            return False
        row = board.seat(job, ctx.led.generation, ctx.seat)
        return row is not None and drive_authz.receipted_role(bk, ctx.led, row) == "judge"
    except Exception:
        return False


def drive_job(cfg: dict, job: str) -> bool:
    """Whether `job` is a drive job on this host (it has a ledger, readable or not). Cannot tell: True (the
    stricter case)."""
    try:
        from swarm.board.autoinit import store_key
        from swarm.supervisor import ledger
        return ledger.exists(store_key(cfg), job)
    except Exception:
        return True
