"""Drive control commands (spec 3.4, 3.6, 4.3; Task 6b): `swarm seat request|list|show|release|cancel`,
`swarm review`, `swarm goal-ready`, `swarm halt` and `swarm unhalt`.

Every command that creates or changes a control record prints, as its last stdout line,
`swarm-record: kind=<kind> id=<n> rev=<n>`; a refusal says why on stderr and exits 1. Drive commands never
spool (spec F18): an unreachable board is an error, not a queued post. Who may act (the caller is the one the
PreToolUse hook attested for this exact command, swarm.person; never an environment variable):
- seat request, seat release, goal-ready: the job's live lead seat (its own main session) or a person;
- seat ack: the job's live lead seat only;
- seat cancel, halt, unhalt, drive repair: a person (person.require: the requester session or a confirmed tty);
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
WITHDRAW_REASON_MAX = 1024     # characters of a --withdraw-artifact reason (the ledger keeps it)
INTEGRATED_MAX = 512           # groups a ledger may record as integrated or withdrawn


class Refuse(Exception):
    """A refused command: the text (without its `refused:` prefix) is printed on stderr."""


def _cli():
    from swarm import cli
    return cli


def _run(fn):
    """Handler wrapper: a Refuse or a person refusal prints and exits 1; a record line is printed by fn."""
    def handler(board, cfg, args):
        person.forget()   # a new command: its caller is taken from its own grant
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
        except ledger.LedgerTooLargeError as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 1
    return handler


def board_key(cfg: dict) -> str:
    from swarm.board.autoinit import store_key
    return store_key(cfg)


def drive_generation(cfg: dict, job: str, warn: bool = False) -> int | None:
    """The job's drive generation from its ledger; None when it is not a drive job or its ledger is unreadable
    (the verdict is then recorded without a drive record: it cannot make the job terminal). `warn` says so on
    stderr for an unreadable ledger."""
    try:
        led = ledger.load(board_key(cfg), job)
    except ledger.LedgerBadError:
        if warn:
            print(f"warning: the drive ledger of {job} is unreadable: this verdict gets no drive record and "
                  "cannot conclude the job until a person repairs the ledger (swarm drive repair)",
                  file=sys.stderr)
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


def _is_lead_seat(name: str) -> bool:
    return name == "lead" or name.startswith("lead~")


def _lead_keys(board, job: str, generation: int) -> set:
    """The agent keys of the job's LIVE lead seat and successors (lead~N) in this generation: a released,
    failed, cancelled or refused lead holds no authority any more."""
    from swarm.board.base import SEAT_CLOSED
    return {s.agent_key for s in board.seats(job, generation) if s.agent_key
            and _is_lead_seat(s.seat) and s.state not in SEAT_CLOSED}


def _caller_lead(board, bk: str, job: str, led) -> tuple:
    """(caller, the lead key the attested caller holds or None, whether it is another enrolled session). The
    caller comes from the hook's grant (person.caller); a subagent (agent_id) is never the lead."""
    who = person.caller(bk)
    if who is None or who.agent_id is not None:
        return who, None, False
    keys = enrolment.agent_keys(bk, who.session_id)
    lead = keys & _lead_keys(board, job, led.generation)
    return who, (sorted(lead)[0] if lead else None), bool(keys)


def _lead_or_person(board, bk: str, job: str, led, env, action: str) -> str:
    """Authority for a seat-staffing command: the lead's own session (attested by its hook), else a person
    (person.require). Other enrolled sessions are refused (they ask the lead on the board)."""
    who, lead, enrolled = _caller_lead(board, bk, job, led)
    if lead is not None:
        return f"lead {lead}"
    if enrolled and not person.requester_of(bk, job, who):
        raise Refuse(f"{action} comes from the job's lead: ask it on the board (post --to @EL)")
    person.require(action, job, board_key=bk, env=env, who=who)
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
    from swarm.board.base import SEAT_CLOSED
    if row.state in SEAT_CLOSED:   # before any ledger write: a refused release changes nothing
        raise Refuse(f"seat {row.seat} is {row.state} already")
    if args.withdraw_artifact:
        group, reason = args.withdraw_artifact.strip(), args.reason.strip()
        if not group or len(group) > 256 or len(reason) > WITHDRAW_REASON_MAX:
            raise Refuse(f"--withdraw-artifact takes a group of 1-256 characters and a --reason of at most "
                         f"{WITHDRAW_REASON_MAX} characters")
        if group not in led.integrated and len(led.integrated) >= INTEGRATED_MAX:
            raise Refuse(f"the ledger already records {INTEGRATED_MAX} groups: a person must look at this job")
        led.integrated[group] = {**(led.integrated.get(group) or {}), "withdrawn": reason}
        led.save()
    new = _set_state(board, row, "released", args.job)
    return _record("seat", new.id, new.revision)


def cmd_seat_cancel(board, cfg, args) -> int:
    bk = board_key(cfg)
    led = _drive_ledger(bk, args.job)
    person.require("seat cancel", args.job, board_key=bk, env=os.environ)   # Claude and Codex seats alike
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
    from swarm.board.base import SEAT_CLOSED
    for s in board.seats(job, generation):
        if s.agent_key == key and _is_lead_seat(s.seat) and s.state not in SEAT_CLOSED:
            return s.seat
    raise Refuse("no live lead seat of this job is held by this session")


def cmd_seat_ack(board, cfg, args) -> int:
    """The lead acknowledges the events it was woken for, up to ID (spec [F4]): its own event mark only,
    which only moves forward, and never past the board's newest message of the job (a mark beyond it would
    silence every later wake). Only the live lead's own session (attested by its hook) may do it."""
    bk = board_key(cfg)
    led = _drive_ledger(bk, args.job)
    _, key, _ = _caller_lead(board, bk, args.job, led)
    if key is None:
        raise Refuse("seat ack comes from the job's live lead session (a person cannot acknowledge for the lead)")
    if args.upto < 0:
        raise Refuse("--upto must be an event id, 0 or more")
    newest = board.last_message_id(args.job)
    if args.upto > newest:
        raise Refuse(f"--upto {args.upto} is past the newest event of {args.job} ({newest})")
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

RECEIPT_HOURS = 24


def _person_receipt(bk: str, led, action: str, how: str) -> None:
    """A person-path command on the owner host writes its own receipt (spec 4.0; the requester session's
    receipt is its PostToolUse hook's): kind `person`, action id `tty:<action>`, bound to the ledger revision
    the command printed as its record id. A failure is reported, not fatal (the ledger change is made)."""
    if how != "tty":
        return
    import time
    from swarm import receipts
    try:
        receipts.write(bk, receipts.Receipt(
            board_key=bk, job=led.job, generation=led.generation, incarnation=led.incarnation,
            action_id=f"tty:{action.replace(' ', '_')}", attempt=0, seat=None, kind="person",
            record_id=str(led.ledger_rev), rev=1,
            content_sha=receipts.content_sha("person", {"action": action, "target": led.job}),
            args_sha=receipts.args_sha(person.cli_argv()), expires_at=time.time() + RECEIPT_HOURS * 3600))
    except (OSError, ValueError) as exc:
        print(f"warning: the person receipt of this {action} was not written: {exc}", file=sys.stderr)


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
    _person_receipt(bk, led, "halt", who)
    return _record("halt", led.ledger_rev, 1)


def cmd_unhalt(board, cfg, args) -> int:
    bk = board_key(cfg)
    _drive_ledger(bk, args.job)
    who = person.require("unhalt", args.job, board_key=bk, env=os.environ)
    led = _drive_ledger(bk, args.job)
    if led.halted is None:
        raise Refuse(f"{args.job} is not halted")
    if str(led.halted.get("reason", "")).startswith(ledger.REPAIR_REASON) and not led.budgets:
        raise Refuse(f"{args.job}'s ledger was rebuilt by a drive repair and has no budgets: set them first, net "
                     f"of the spend the repair reported (swarm drive budget --job {args.job} KEY=VALUE ..., or "
                     "re-activate with --budget), then unhalt")
    now = board.now()
    since = _dt.datetime.fromisoformat(led.halted["at"])
    if since.tzinfo is None:
        since = since.replace(tzinfo=_dt.timezone.utc)
    led.halted_total_seconds += max(0.0, (now - since).total_seconds())
    led.halted = None
    led.save()
    _person_receipt(bk, led, "unhalt", who)
    return _record("unhalt", led.ledger_rev, 1)


# --- drive repair ------------------------------------------------------------------------------

CLOSED_VERDICTS = {"completed": "met", "failed": "unreachable"}


def _board_finished(board, job: str) -> str | None:
    """The verdict a closed board job implies (None: open or unknown). The board is untrusted: this only ever
    keeps a job finished, it never makes one run."""
    js = board.job_status(job)
    if js is None or js.status == "active":
        return None
    return CLOSED_VERDICTS.get(js.status, "unreachable")


def cmd_drive_repair(board, cfg, args) -> int:
    """Person path (spec F2): a drive ledger that does not read is moved aside (never deleted) and a fresh one
    takes its place, under one lock (ledger.repair). The generation comes from the archived ledger, else from a
    person's --generation, never from the board. A finished job stays finished; otherwise the fresh ledger is
    halted and unhalt wants budgets set first. Everything not recovered is listed on stderr."""
    bk = board_key(cfg)
    who = person.require("drive repair", args.job, board_key=bk, env=os.environ)
    data = board.job_data(args.job) or {}
    try:
        rep = ledger.repair(bk, args.job, at=_now_iso(board), by=f"person ({who})", generation=args.generation,
                            claimed=data.get("drive.enabled") == "1",
                            board_finished=_board_finished(board, args.job))
    except ledger.LedgerExistsError as exc:
        raise Refuse(f"{exc}: run the repair again")
    except ValueError as exc:
        raise Refuse(str(exc))
    if rep.archived:
        print(f"bad drive ledger moved aside as {rep.archived}")
    elif rep.source:
        print(f"finishing an interrupted repair from {rep.source}")
    raw = data.get("drive.generation", "")
    if raw != str(rep.ledger.generation):
        print(f"warning: the board says generation {raw or '(none)'}, the repaired ledger is generation "
              f"{rep.ledger.generation} (the ledger wins; the driver rewrites the board copy)", file=sys.stderr)
    print(f"WARNING: drive repair of {args.job}: recovered {', '.join(rep.kept) or 'nothing'}. NOT recovered:",
          file=sys.stderr)
    for line in rep.lost:
        print(f"  - {line}", file=sys.stderr)
    if rep.finished:
        print(f"{args.job} had finished: the repaired ledger stays terminal "
              f"({rep.ledger.terminal.get('verdict')}); nothing will run", file=sys.stderr)
    else:
        print(f"{args.job} is halted: set the budgets (net of the spend above), check the pinned requirements, "
              f"then `swarm unhalt --job {args.job}`", file=sys.stderr)
    _person_receipt(bk, rep.ledger, "drive repair", who)
    return _record("halt", rep.ledger.ledger_rev, 1)


# --- registration ------------------------------------------------------------------------------

# One `drive` parser tree for every task that adds a `swarm drive <cmd>` (6b: repair; 6a: budget, models).
# A task adds its sub-command with drive_subparsers(sub).add_parser(...) and add_drive_command(name, handler):
# argparse refuses a second `drive` parser, so nobody calls sub.add_parser("drive") directly.
DRIVE_COMMANDS: dict = {}


def drive_subparsers(sub):
    """The `drive` sub-command's subparsers of the CLI's subparsers `sub`, made on first use (kept on `sub`)."""
    tree = getattr(sub, "_swarm_drive_tree", None)
    if tree is None:
        dr = sub.add_parser("drive", help="drive ledger repair and running-job changes")
        tree = dr.add_subparsers(dest="dcmd", required=True)
        sub._swarm_drive_tree = tree
    return tree


def add_drive_command(name: str, handler, *, board: bool = True) -> None:
    """Register `swarm drive <name>`: handler(board, cfg, args), or handler(cfg, args) when `board` is False."""
    DRIVE_COMMANDS[name] = handler if board else (lambda _board, cfg, args: handler(cfg, args))


def cmd_drive(board, cfg, args) -> int:
    return DRIVE_COMMANDS[args.dcmd](board, cfg, args) or 0


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
    rp = drive_subparsers(sub).add_parser(
        "repair", help="move a bad drive ledger aside; a fresh one starts halted (a person)")
    rp.add_argument("--job", required=True)
    rp.add_argument("--generation", type=int, metavar="N",
                    help="the job's generation, when the bad ledger no longer tells it (never read from the board)")
    add_drive_command("repair", cmd_drive_repair)
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
