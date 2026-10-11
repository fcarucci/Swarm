"""Drive control commands (spec 3.4, 3.6, 4.3; Task 6b): `swarm seat request|list|show|release|cancel`,
`swarm review`, `swarm goal-ready`, `swarm halt` and `swarm unhalt`.

Every command that creates or changes a control record prints, as its last stdout line,
`swarm-record: kind=<kind> id=<n> rev=<n>`; a refusal says why on stderr and exits 1. Drive commands never
spool (spec F18): an unreachable board is an error, not a queued post. Who may act:
- seat request, seat release, goal-ready: the job's lead seat (its enrolled session) or a person;
- seat cancel, halt, unhalt: a person (person.require: the requester session or a confirmed tty);
- review: an enrolled agent of the job, by its key.
Kept out of cli.py (a 256 KiB file cap): cli.py only calls register()."""
from __future__ import annotations

import datetime as _dt
import hashlib
import os
import re
import sys

from swarm import enrolment, person
from swarm.supervisor import ledger

RECORD = "swarm-record: kind={kind} id={id} rev={rev}"
REVIEW_OUTCOMES = ("verified", "changes")
DEFAULT_MODELS = {"lead": "opus", "judge": "opus", "security": "opus", "engineer": "haiku",
                  "worker": "haiku", "integrator": "haiku", "reviewer": "sonnet", "qa": "sonnet"}
REPORT_MAX = 64 * 1024
REQUIREMENT_MAX = 1024 * 1024
CANDIDATE = re.compile(r"(?:report:)?[^\s@]+@[0-9a-f]{7,64}")


class Refuse(Exception):
    """A refused command: the text (without its `refused:` prefix) is printed on stderr."""


def _cli():
    from swarm import cli
    return cli


def _run(fn):
    """Handler wrapper: a Refuse or a person refusal prints and exits 1; a record line is printed by fn."""
    def handler(board, cfg, args):
        try:
            return fn(board, cfg, args) or 0
        except (Refuse, person.PersonRefused) as exc:
            msg = str(exc)
            print(msg if msg.startswith("refused") else f"refused: {msg}", file=sys.stderr)
            return 1
        except ledger.LedgerConflict:
            print("refused: the drive ledger changed meanwhile; run the command again", file=sys.stderr)
            return 1
        except ledger.LedgerBadError:
            print("refused: the drive ledger of this job is unreadable; the job holds until a person "
                  "repairs it", file=sys.stderr)
            return 1
    return handler


def board_key(cfg: dict) -> str:
    from swarm.board.autoinit import store_key
    return store_key(cfg)


def drive_generation(cfg: dict, job: str) -> int | None:
    """The job's drive generation from its ledger; None when it is not a drive job or its ledger is unreadable
    (the verdict is then recorded without a drive record: it cannot make the job terminal)."""
    try:
        led = ledger.load(board_key(cfg), job)
    except ledger.LedgerBadError:
        return None
    return led.generation if led is not None else None


def _now_iso(board) -> str:
    return board.now().isoformat()


def _record(kind: str, rec_id: int, rev: int) -> int:
    print(RECORD.format(kind=kind, id=int(rec_id), rev=int(rev)))
    return 0


def _owner_roots(bk: str, job: str) -> list:
    """The job's authorized work roots, from the host-private owner record. None of them recorded (or
    an owner record without them) means no seat workdir and no requirement is accepted: fails closed."""
    owner = enrolment.find_job_owner(bk, job)
    roots = getattr(owner, "roots", None) if owner is not None else None
    return list(roots) if isinstance(roots, (list, tuple)) else []


def _drive_ledger(bk: str, job: str):
    led = ledger.load(bk, job)
    if led is None:
        raise Refuse(f"{job} is not a drive job (no drive ledger): activate it with --goal")
    return led


def _within(path: str, root: str) -> bool:
    """Whether `path` is `root` or lies under it (both already real paths; case and separators per OS)."""
    p, r = os.path.normcase(path), os.path.normcase(root)
    try:
        return os.path.commonpath([p, r]) == r
    except ValueError:   # another drive, or one absolute and one not
        return False


TMP_ROOTS = ("/tmp", "/private/tmp")   # a seat's workdir is never under these (tests patch it: the suite sandbox is under /tmp)


def _under_tmp(path: str) -> bool:
    return any(_within(path, t) for t in TMP_ROOTS)


def _check_workdir(bk: str, job: str, workdir: str) -> str:
    real = os.path.realpath(os.path.expanduser(workdir))
    if _under_tmp(real):
        raise Refuse("a seat's workdir is never /tmp: use a checkout under ~/src or ~/work")
    roots = [os.path.realpath(r) for r in _owner_roots(bk, job)]
    if not any(_within(real, r) for r in roots):
        raise Refuse(f"the workdir {real} is outside the job's work roots (swarm activate --allow-workdir)")
    return real


def _caller_agent(bk: str, env) -> str | None:
    """The agent key of an enrolled caller session on this board (any job), or None."""
    sid = person.session_of(env)
    return enrolment.agent_sessions(bk).get(sid) if sid else None


def _lead_keys(board, job: str, generation: int) -> set:
    """The agent keys of the job's lead seat and its successors (lead~N) in this generation."""
    return {s.agent_key for s in board.seats(job, generation) if s.agent_key
            and (s.seat == "lead" or s.seat.startswith("lead~"))}


def _lead_or_person(board, bk: str, job: str, led, env, action: str) -> str:
    """Authority for a seat-staffing command: the lead's enrolled session, else a person. Other enrolled
    sessions are refused (they ask the lead on the board)."""
    key = _caller_agent(bk, env)
    if key is not None:
        if key not in _lead_keys(board, job, led.generation):
            raise Refuse(f"{action} comes from the job's lead: ask it on the board (post --to @EL)")
        return f"lead {key}"
    person.require(action, job, board_key=bk, env=env)
    return "person"


def _model_for(cfg: dict, role: str, given: str | None) -> str:
    drive = cfg.get("drive") or {}
    allowed = drive.get("allowed_models") or ["haiku", "sonnet", "opus"]
    key = "lead" if role == "engineering_lead" else role
    model = given or (drive.get("models") or {}).get(key) or DEFAULT_MODELS.get(key)
    if not model:
        raise Refuse(f"no default model for role {role}: give --model")
    if model not in allowed:
        raise Refuse(f"model {model} is not in [drive] allowed_models")
    return model


def _seat_row(board, job: str, generation: int, seat: str):
    row = board.seat(job, generation, seat)
    if row is None:
        raise Refuse(f"no seat {seat} in {job}")
    return row


def _set_state(board, row, state: str, job: str, **fields):
    try:
        new = board.set_seat_state(row.id, row.revision, state, job=job, **fields)
    except ValueError as exc:
        raise Refuse(str(exc))
    if new is None:
        raise Refuse(f"seat {row.seat} changed meanwhile: look at it (swarm seat show) and retry")
    return new


# --- seats -------------------------------------------------------------------------------------

def cmd_seat_request(board, cfg, args) -> int:
    bk = board_key(cfg)
    led = _drive_ledger(bk, args.job)
    who = _lead_or_person(board, bk, args.job, led, os.environ, "seat request")
    workdir = _check_workdir(bk, args.job, args.workdir or os.getcwd())
    model = _model_for(cfg, args.role, args.model)
    seat = board.request_seat(args.job, led.generation, args.seat, role=args.role, harness=args.harness,
                              model=model, workdir=workdir, brief_path=args.brief, title=args.title,
                              task_ref=args.task, complexity=args.complexity or "standard", requested_by=who)
    return _record("seat", seat.id, seat.revision)


def cmd_seat_release(board, cfg, args) -> int:
    bk = board_key(cfg)
    led = _drive_ledger(bk, args.job)
    _lead_or_person(board, bk, args.job, led, os.environ, "seat release")
    if args.withdraw_artifact and not (args.reason or "").strip():
        raise Refuse("--withdraw-artifact needs --reason: say why the artifact is withdrawn")
    row = _seat_row(board, args.job, led.generation, args.seat)
    if args.withdraw_artifact:
        group = args.withdraw_artifact
        led.integrated[group] = {**(led.integrated.get(group) or {}), "withdrawn": args.reason.strip()}
        led.save()
    new = _set_state(board, row, "released", args.job)
    return _record("seat", new.id, new.revision)


def cmd_seat_cancel(board, cfg, args) -> int:
    bk = board_key(cfg)
    led = _drive_ledger(bk, args.job)
    person.require("seat cancel", args.job, board_key=bk, env=os.environ)
    row = _seat_row(board, args.job, led.generation, args.seat)
    new = _set_state(board, row, "cancelled", args.job)
    return _record("seat", new.id, new.revision)


def cmd_seat_list(board, cfg, args) -> int:
    bk = board_key(cfg)
    led = _drive_ledger(bk, args.job)
    for s in board.seats(args.job, led.generation):
        print(f"{s.seat}\t{s.state}\t{s.role}\t{s.model}\trev {s.revision}\t{s.workdir}")
    return 0


def cmd_seat_show(board, cfg, args) -> int:
    bk = board_key(cfg)
    led = _drive_ledger(bk, args.job)
    s = _seat_row(board, args.job, led.generation, args.seat)
    for k in ("seat", "role", "state", "model", "harness", "workdir", "brief_path", "revision", "last_outcome"):
        print(f"{k}: {getattr(s, k)}")
    return 0


def _lead_seat_name(board, job: str, generation: int, key: str) -> str:
    for s in board.seats(job, generation):
        if s.agent_key == key and (s.seat == "lead" or s.seat.startswith("lead~")):
            return s.seat
    raise Refuse("no lead seat of this job is held by this session")


def cmd_seat_ack(board, cfg, args) -> int:
    """The lead acknowledges the events it was woken for, up to ID (spec [F4]): its own event mark only,
    which only moves forward. Only the lead's enrolled session may do it."""
    bk = board_key(cfg)
    led = _drive_ledger(bk, args.job)
    key = _caller_agent(bk, os.environ)
    if key is None or key not in _lead_keys(board, args.job, led.generation):
        raise Refuse("seat ack comes from the job's lead session (a person cannot acknowledge for the lead)")
    if args.upto < 0:
        raise Refuse("--upto must be an event id, 0 or more")
    mark = f"event:{_lead_seat_name(board, args.job, led.generation, key)}"
    led.watermarks[mark] = max(led.watermarks.get(mark, 0), args.upto)
    led.save()
    return _record("ack", led.ledger_rev, 1)


SEAT_COMMANDS = {"request": cmd_seat_request, "release": cmd_seat_release, "cancel": cmd_seat_cancel,
                 "list": cmd_seat_list, "show": cmd_seat_show, "ack": cmd_seat_ack}


def cmd_seat(board, cfg, args) -> int:
    return SEAT_COMMANDS[args.scmd](board, cfg, args) or 0


# --- review and goal-ready ---------------------------------------------------------------------

def _read_regular(path: str, limit: int, what: str) -> bytes:
    real = os.path.realpath(os.path.expanduser(path))
    try:
        st = os.stat(real)
    except OSError as exc:
        raise Refuse(f"{what}: cannot read {path}: {exc.strerror or exc}")
    if not os.path.isfile(real) or st.st_size > limit:
        raise Refuse(f"{what}: {path} is not a regular file of at most {limit} bytes")
    with open(real, "rb") as fh:
        return fh.read(limit)


def cmd_review(board, cfg, args) -> int:
    bk = board_key(cfg)
    key = args.key
    if not key or not enrolment.owns(bk, key, args.job):
        raise Refuse("a review comes from an enrolled agent of this job (give its --key)")
    name = board.active_agent_name(key)
    if not name:
        raise Refuse("the reviewing agent is not active on this job")
    data = _read_regular(args.report, REPORT_MAX, "review report")
    if not data.strip():
        raise Refuse("the review report is empty")
    artifact = args.artifact.strip()
    if not artifact or any(c.isspace() for c in artifact):
        raise Refuse("--artifact must be one reference without spaces")
    verdict = args.outcome.upper()
    res = board.post(args.job, name, f"REVIEW {artifact} {verdict} {args.report}", None, agent_key=key)
    return _record("review", res.id, 1)


def blob_id(data: bytes) -> str:
    """The git object id of a file's content (no git run): sha1 of "blob <len>\\0" + data."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def requirements_digest(pins: dict) -> str:
    return hashlib.sha256("".join(f"{p}@{b}\n" for p, b in sorted(pins.items())).encode()).hexdigest()


def cmd_goal_ready(board, cfg, args) -> int:
    bk = board_key(cfg)
    led = _drive_ledger(bk, args.job)
    who = _lead_or_person(board, bk, args.job, led, os.environ, "goal-ready")
    if not CANDIDATE.fullmatch(args.candidate or ""):
        raise Refuse("--candidate must be REF@SHA or report:<path>@<blob id>")
    pins = dict(led.pinned_requirements)
    roots = [os.path.realpath(r) for r in _owner_roots(bk, args.job)]
    for path in args.add_requirements or []:
        real = os.path.realpath(os.path.expanduser(path))
        if not any(_within(real, r) for r in roots):
            raise Refuse(f"requirement {path} is outside the job's work roots")
        blob = blob_id(_read_regular(path, REQUIREMENT_MAX, "requirement"))
        if real in pins and pins[real] != blob:
            raise Refuse(f"requirement {path} is pinned at another blob: a pinned requirement cannot be replaced")
        pins[real] = blob
    if not pins:
        raise Refuse("a goal-ready needs at least one pinned requirement (--add-requirements PATH)")
    if pins != led.pinned_requirements:
        led.pinned_requirements = pins
        led.save()
    req = requirements_digest(pins)
    name = board.active_agent_name(who.split(" ", 1)[1]) if who.startswith("lead ") else "person"
    res = board.post(args.job, name or "person", f"GOAL-READY {args.candidate} req={req}", None)
    return _record("goal_ready", res.id, 1)


# --- halt and unhalt ---------------------------------------------------------------------------

def cmd_halt(board, cfg, args) -> int:
    bk = board_key(cfg)
    _drive_ledger(bk, args.job)
    who = person.require("halt", args.job, board_key=bk, env=os.environ)
    led = _drive_ledger(bk, args.job)
    if led.terminal is not None:
        raise Refuse(f"{args.job} is finished; nothing to halt")
    if led.halted is not None:
        raise Refuse(f"{args.job} is already halted")
    halt = {"by": f"person ({who})", "at": _now_iso(board)}
    if (args.reason or "").strip():
        halt["reason"] = args.reason.strip()
    led.halted = halt
    led.save()
    return _record("halt", led.ledger_rev, 1)


def cmd_unhalt(board, cfg, args) -> int:
    bk = board_key(cfg)
    _drive_ledger(bk, args.job)
    person.require("unhalt", args.job, board_key=bk, env=os.environ)
    led = _drive_ledger(bk, args.job)
    if led.halted is None:
        raise Refuse(f"{args.job} is not halted")
    now = board.now()
    since = _dt.datetime.fromisoformat(led.halted["at"])
    if since.tzinfo is None:
        since = since.replace(tzinfo=_dt.timezone.utc)
    led.halted_total_seconds += max(0.0, (now - since).total_seconds())
    led.halted = None
    led.save()
    return _record("unhalt", led.ledger_rev, 1)


# --- drive repair ------------------------------------------------------------------------------

def cmd_drive_repair(board, cfg, args) -> int:
    """Person path (spec F2): a drive ledger that does not read is moved aside (never deleted) and a fresh one
    takes its place at the job's board generation. The fresh ledger is halted, so nothing launches until a
    person sets the budgets and unhalts. Pinned requirements and integrated groups of the bad ledger are not
    recovered: the goal judge then needs them pinned again (GOAL-READY --add-requirements)."""
    bk = board_key(cfg)
    who = person.require("drive repair", args.job, board_key=bk, env=os.environ)
    try:
        led = ledger.load(bk, args.job)
    except ledger.LedgerBadError:
        led = None
    if led is not None:
        raise Refuse(f"{args.job}'s drive ledger reads fine: nothing to repair")
    try:
        moved = ledger.archive_bad(bk, args.job)
    except ValueError as exc:
        raise Refuse(str(exc))
    print(f"bad drive ledger moved aside as {moved}")
    raw = (board.job_data(args.job) or {}).get("drive.generation", "")
    generation = int(raw) if raw.isdigit() and int(raw) >= 1 else 1
    fresh = ledger.create(bk, args.job, budgets={}, activated_at=_now_iso(board))
    fresh.generation = fresh.generation_requested = generation
    fresh.halted = {"by": f"person ({who})", "at": _now_iso(board),
                    "reason": "drive repair: set the budgets, then unhalt"}
    fresh.save()
    return _record("halt", fresh.ledger_rev, 1)


# --- registration ------------------------------------------------------------------------------

def register(sub, handlers: dict) -> None:
    """Add the control parsers to the subparsers `sub` and their handlers to `handlers` (cli's
    BOARD_COMMANDS). Called once from cli.py."""
    h = sub.add_parser("halt", help="a person stops a drive job on purpose (until unhalt or cancel)")
    h.add_argument("--job", required=True); h.add_argument("--reason")
    u = sub.add_parser("unhalt", help="a person resumes a halted drive job")
    u.add_argument("--job", required=True)
    s = sub.add_parser("seat", help="drive seats: request, list, show, release or cancel a seat")
    ss = s.add_subparsers(dest="scmd", required=True)
    rq = ss.add_parser("request", help="ask for a seat (the lead or a person)")
    rq.add_argument("--job", required=True); rq.add_argument("--seat", required=True)
    rq.add_argument("--role", required=True); rq.add_argument("--brief", required=True)
    rq.add_argument("--model"); rq.add_argument("--harness", default="claude", choices=("claude", "codex"))
    rq.add_argument("--workdir"); rq.add_argument("--title"); rq.add_argument("--task")
    rq.add_argument("--complexity", choices=("simple", "standard", "complex"))
    for name in ("list", "show", "release", "cancel"):
        p = ss.add_parser(name)
        p.add_argument("--job", required=True)
        if name != "list":
            p.add_argument("--seat", required=True)
        if name == "release":
            p.add_argument("--withdraw-artifact", metavar="GROUP")
            p.add_argument("--reason")
        if name == "cancel":
            p.add_argument("--reason")
    ak = ss.add_parser("ack", help="the lead acknowledges the events it was woken for, up to ID")
    ak.add_argument("--job", required=True); ak.add_argument("--upto", type=int, required=True)
    dr = sub.add_parser("drive", help="drive ledger repair (a person)")
    ds = dr.add_subparsers(dest="dcmd", required=True)
    rp = ds.add_parser("repair", help="move a bad drive ledger aside; a fresh one starts halted")
    rp.add_argument("--job", required=True)
    rv = sub.add_parser("review", help="post a structured review of an artifact (an enrolled reviewer)")
    rv.add_argument("--job", required=True); rv.add_argument("--artifact", required=True)
    rv.add_argument("outcome", choices=REVIEW_OUTCOMES); rv.add_argument("--report", required=True)
    rv.add_argument("--key", help="the reviewing agent's key")
    gr = sub.add_parser("goal-ready", help="declare the goal ready for the judge (the lead or a person)")
    gr.add_argument("--job", required=True); gr.add_argument("--candidate", required=True)
    gr.add_argument("--add-requirements", action="append", metavar="PATH")
    handlers.update({"halt": _run(cmd_halt), "unhalt": _run(cmd_unhalt), "seat": _run(cmd_seat),
                     "review": _run(cmd_review), "goal-ready": _run(cmd_goal_ready),
                     "drive": _run(cmd_drive)})


def cmd_drive(board, cfg, args) -> int:
    return DRIVE_COMMANDS[args.dcmd](board, cfg, args) or 0


DRIVE_COMMANDS = {"repair": cmd_drive_repair}
