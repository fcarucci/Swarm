"""Drive control commands: `swarm run`,
`swarm review`, `swarm goal-ready`, `swarm halt` and `swarm unhalt`.

Every command that creates or changes a control record prints, as its last stdout line,
`swarm-record: kind=<kind> id=<n> rev=<n>`; a refusal says why on stderr and exits 1. Drive commands never
spool (spec F18): an unreachable board is an error, not a queued post. Who may act (the caller is the one the
PreToolUse hook attested for this exact command, swarm.person; never an environment variable):
- goal-ready: the job's live lead launch (its own main session) or a person;
- halt, unhalt, drive repair: a person (person.require: the requester session or a confirmed tty);
- review: an enrolled agent of the job, by its key.
Kept out of cli.py (a 256 KiB file cap): cli.py only calls register()."""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import os
import re
import sys

from swarm import enrolment, person
from swarm.supervisor import ledger
from swarm.supervisor.drive_authz import requirements_digest  # noqa: F401  (the one copy: Task 5's)

RECORD = "swarm-record: kind={kind} id={id} rev={rev}"
REVIEW_OUTCOMES = ("verified", "changes")
DEFAULT_MODELS = {"lead": "opus", "judge": "opus", "security": "opus", "engineer": "haiku",
                  "worker": "haiku", "integrator": "haiku", "reviewer": "sonnet", "qa": "sonnet"}
REPORT_MAX = 64 * 1024
REQUIREMENT_MAX = 1024 * 1024
CANDIDATE = re.compile(r"(?:report:)?[^\s@]+@[0-9a-f]{7,64}")
PINS_MAX = 256                 # requirement files a job may pin (N5)
PIN_PATH_MAX = 256             # characters of a pinned path (the ledger's key limit)
CLI_LEDGER_MAX = 2 * 1024 * 1024   # a CLI write keeps the ledger under this: headroom up to ledger.MAX_BYTES
                                   # stays for the driver's own writes (N5)
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


def drive_record(cfg: dict, job: str, kind: str, rec_id: int, rev: int = 1) -> None:
    """Print the record line of a board command (done, post --to, ask, answer) when `job` is a drive job (a
    readable drive ledger on this host): the PostToolUse hook receipts the record by it (drivegate). Other jobs
    print none (their output is unchanged). Never raises."""
    try:
        if ledger.load(board_key(cfg), job) is None:
            return
    except Exception:
        return
    _record(kind, rec_id, rev)


def _record(kind: str, rec_id: int, rev: int) -> int:
    print(RECORD.format(kind=kind, id=int(rec_id), rev=int(rev)))
    return 0


def _owner_roots(bk: str, job: str) -> list:
    """The job's authorized work roots, from the host-private owner record. None of them recorded (or
    an owner record without them) means no launch workdir and no requirement is accepted: fails closed."""
    owner = enrolment.find_job_owner(bk, job)
    # the roots are in the owner record's `owner` mapping (6a's format), not an attribute of the Record
    data = getattr(owner, "owner", None) if owner is not None else None
    roots = data.get("roots") if isinstance(data, dict) else None
    return list(roots) if isinstance(roots, (list, tuple)) else []


def _check_headroom(led) -> None:
    """Refuse a CLI change that would leave the ledger over CLI_LEDGER_MAX: the room above it, up to
    ledger.MAX_BYTES, is the driver's, so a lead's pins, acks or withdrawals can never wedge the driver."""
    size = ledger.serialized_size(led)
    if size > CLI_LEDGER_MAX:
        raise Refuse(f"the drive ledger would be {size} bytes, over the {CLI_LEDGER_MAX} a command may fill "
                     "(the rest is the driver's): a person must look at this job")


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


TMP_ROOTS = ("/tmp", "/private/tmp")   # a launch's workdir is never under these (tests patch it: the suite sandbox is under /tmp)


def _under_tmp(path: str) -> bool:
    return any(_within(path, t) for t in TMP_ROOTS)


def _check_workdir(bk: str, job: str, workdir: str) -> str:
    real = os.path.realpath(os.path.expanduser(workdir))
    if _under_tmp(real):
        raise Refuse("a launch's workdir is never /tmp: use a checkout under ~/src or ~/work")
    roots = [os.path.realpath(r) for r in _owner_roots(bk, job)]
    if not any(_within(real, r) for r in roots):
        raise Refuse(f"the workdir {real} is outside the job's work roots (swarm activate --allow-workdir)")
    return real


def _is_lead_launch(name: str) -> bool:
    return name == "lead" or name.startswith("lead~")


def _caller(bk: str, job: str, led, args):
    """The caller the PreToolUse hook attested for this command (person.caller; None: none)."""
    return person.caller(bk, job, led, args)


def _live_lead(board, job: str, led, who) -> str | None:
    """The attested caller's launch name when it is a LIVE lead launch (lead, lead~N) of this generation."""
    from swarm.board.base import LAUNCH_CLOSED
    if who is None or who.launch is None or not _is_lead_launch(who.launch):
        return None
    row = board.get_launch(job, led.generation, who.launch)
    return who.launch if row is not None and row.state not in LAUNCH_CLOSED else None


def _lead_or_person(board, bk: str, job: str, led, env, action: str, args) -> str:
    """Authority for a launch-staffing command: the live lead launch (attested by its hook), else a person
    (person.require). Other launches are refused (they ask the lead on the board)."""
    who = _caller(bk, job, led, args)
    lead = _live_lead(board, job, led, who)
    if lead is not None:
        row = board.get_launch(job, led.generation, lead)
        return f"lead {row.agent_key or lead}"
    if who is not None and not who.requester:
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


def cmd_run(board, cfg, args) -> int:
    """The trusted CLI/broker caller queues a launch; no authorization receipt."""
    from swarm.supervisor import runner
    if args.harness == "codex":
        from swarm.supervisor.launch import codex_command
        try:
            codex_command(cfg, resolve=True)
        except ValueError as exc:
            raise Refuse(str(exc)) from exc
    bk = board_key(cfg)
    led = _drive_ledger(bk, args.job)
    workdir = _check_workdir(bk, args.job, args.workdir or os.getcwd())
    # The same descriptor walk, roots, dot-dir, home, ownership and config rules as at launch.
    fd, why = runner.open_workdir(cfg, workdir)
    if fd is None:
        raise Refuse(why)
    os.close(fd)
    caller = _caller(bk, args.job, led, args)
    queued_by = led.requester_session or "requester"
    if caller is not None and caller.launch:
        source = board.get_launch(args.job, led.generation, caller.launch)
        source_run = next((r for r in board.restarts(args.job)
                           if source is not None and r.launch_id == source.launch_id), None)
        keys = {source.agent_key} if source else set()
        if source_run:
            keys.update((source_run.new_agent_key, source_run.old_agent_key))
        queued_by = next((a.name for a in board.agents(args.job) if a.agent_key in keys), caller.launch)
    if board.get_launch(args.job, led.generation, args.name) is not None:
        from swarm.supervisor.driver_exec import mutate
        key = f"requeue:{led.generation}:{args.name}"
        mutate(led, lambda l: l.watermarks.__setitem__(key, l.watermarks.get(key, 0) + 1))
    row = board.queue_launch(args.job, led.generation, args.name, role=args.role, harness=args.harness,
                             model=_model_for(cfg, args.role, args.model), workdir=workdir,
                             brief=args.brief, queued_by=queued_by)
    if args.name == "lead" and (caller is None or caller.requester):
        from swarm.supervisor.driver_exec import mutate
        def reset(l):
            l.ruling_due_at, l.ruling_attempts = None, 0
            l.rounds_without_progress, l.errors = 0, 0
        mutate(led, reset)
    board.set_job_data(args.job, f"drive.skip.{led.generation}.{args.name}", None)
    print(f"{row.name}: {row.state}")
    from swarm.drivegate import kick
    kick(cfg=cfg)
    return 0


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
    content = getattr(args, "report_content", None)
    inline = getattr(args, "report_text", None)
    if (inline is None) == (args.report is None):
        raise Refuse("give the review report as --report-text TEXT (or --report FILE), exactly one")
    if inline is not None:
        data = inline.encode("utf-8")
        if len(data) > REPORT_MAX:
            raise Refuse(f"the review report is over {REPORT_MAX} bytes")
    else:
        data = _read_regular(content or args.report, REPORT_MAX, "review report")
    if not data.strip():
        raise Refuse("the review report is empty")
    artifact = args.artifact.strip()
    if not artifact or any(c.isspace() for c in artifact):
        raise Refuse("--artifact must be one reference without spaces")
    verdict = args.outcome.upper()
    res = board.post(args.job, name, f"REVIEW {artifact} {verdict} {review_ref(args.report)}", None, agent_key=key)
    if content or inline is not None:
        _keep_review_report(args.job, res.id, data)
    return _record("review", res.id, 1)


REVIEW_REPORTS = "review-reports"
INLINE_REPORT = "(report inline)"   # the REVIEW post's report reference of a --report-text review


def review_ref(report: str | None) -> str:
    """What a REVIEW post names as its report: the path as typed, or INLINE_REPORT (the text is kept privately)."""
    return report if report is not None else INLINE_REPORT


def review_report_path(job: str, message_id: int):
    """Where a brokered review's report is kept (the supervisor's private dir): readable later by the driver and a
    person, whatever happens to the launch's work tree."""
    from swarm.supervisor.settings import private_dir
    return private_dir() / REVIEW_REPORTS / f"{hashlib.sha256(job.encode()).hexdigest()[:16]}-{int(message_id)}.md"


def _keep_review_report(job: str, message_id: int, data: bytes) -> None:
    from swarm import safefs
    from swarm.supervisor import privfs
    p = review_report_path(job, message_id)
    with privfs.dir_fd() as d:
        sub = safefs.open_sub(d, REVIEW_REPORTS, strict_mode=0o700)
        try:
            privfs.write_atomic(sub, p.name, data, durable=True)
        finally:
            os.close(sub)


def blob_id(data: bytes) -> str:
    """The git object id of a file's content (no git run): sha1 of "blob <len>\\0" + data."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()




def pins_for(bk: str, job: str, led, paths, pinned=()) -> dict:
    """The pin map `led` would hold with `paths` added (nothing changed): each path must lie under the job's
    roots, be a regular file of at most REQUIREMENT_MAX bytes and keep the blob it is pinned at, and the job
    pins at most PINS_MAX files. `pinned`: a launch broker's "PATH\nFILE" pairs (the content it read from PATH)."""
    pins = dict(led.pinned_requirements)
    roots = [os.path.realpath(r) for r in _owner_roots(bk, job)]
    items = [(p, None) for p in paths]
    for pair in pinned:
        path, sep, content = str(pair).partition("\n")
        if not sep or not os.path.isabs(path) or os.path.normpath(path) != path:
            raise Refuse("a brokered requirement must be an absolute, normalised path")
        items.append((path, content))
    for path, content in items:
        # a brokered PATH is resolved too (the same key either way; on Windows a short 8.3 name or another case
        # of the job's root still matches): the broker read it with no link on the way, and a link swapped in
        # since resolves to where it points, so it is pinned only when that lies in the job's roots
        real = os.path.realpath(os.path.expanduser(path)) if content is None else os.path.realpath(path)
        if len(real) > PIN_PATH_MAX:
            raise Refuse(f"requirement path {real[:80]}... is over {PIN_PATH_MAX} characters: pin a shorter path")
        if not any(_within(real, r) for r in roots):
            raise Refuse(f"requirement {path} is outside the job's work roots")
        blob = blob_id(_read_regular(content or path, REQUIREMENT_MAX, "requirement"))
        if real in pins and pins[real] != blob:
            raise Refuse(f"requirement {path} is pinned at another blob: a pinned requirement cannot be replaced")
        pins[real] = blob
        if len(pins) > PINS_MAX:
            raise Refuse(f"a job pins at most {PINS_MAX} requirement files: name the requirements, not every file")
    return pins


def core_pins(bk: str, job: str, led, paths, pinned=()) -> bool:
    """The ledger side of pinning (no save): set the pins pins_for() gives. True when the ledger changed."""
    pins = pins_for(bk, job, led, paths, pinned)
    if pins == led.pinned_requirements:
        return False
    led.pinned_requirements = pins
    _check_headroom(led)
    return True


def cmd_goal_ready(board, cfg, args) -> int:
    bk = board_key(cfg)
    led = _drive_ledger(bk, args.job)
    if not CANDIDATE.fullmatch(args.candidate or ""):
        raise Refuse("--candidate must be REF@SHA or report:<path>@<blob id>")
    pinned = getattr(args, "add_requirements_pinned", None) or []
    if person.codex_session(os.environ) and pins_for(bk, args.job, led, args.add_requirements or [], pinned) != \
            led.pinned_requirements:
        # new pins write the ledger: the driver applies them; the lead posts GOAL-READY once they are applied
        return control_request(board, cfg, args)
    # Codex, no new pins: a board-only record, which counts once the hook's goal_ready receipt names the caller
    who = ("codex" if person.codex_session(os.environ)
           else _lead_or_person(board, bk, args.job, led, os.environ, "goal-ready", args))
    changed = core_pins(bk, args.job, led, args.add_requirements or [], pinned)
    pins = led.pinned_requirements
    if not pins:
        raise Refuse("a goal-ready needs at least one pinned requirement (--add-requirements PATH)")
    if changed:
        led.save()
    req = requirements_digest(pins)
    name = (board.active_agent_name(who.split(" ", 1)[1]) if who.startswith("lead ")
            else "codex session" if who == "codex" else "person")
    res = board.post(args.job, name or "person", f"GOAL-READY {args.candidate} req={req}", None)
    return _record("goal_ready", res.id, 1)


# --- halt and unhalt ---------------------------------------------------------------------------

PERSON_RECEIPT_DAYS = 30   # long enough for any pass of the driver to see it (a cancel is read at its close)


def person_record_id(action: str, ref) -> str:
    """The record id of a person receipt: `<action>:<ref>`, the action with `_` for spaces (halt, unhalt,
    drive_repair, launch_cancel, deactivate) and `ref` what the command printed as its record id (the ledger
    revision for halt, unhalt and drive repair; the launch row id for a cancel; the generation for deactivate)."""
    return f"{action.replace(' ', '_')}:{ref}"


def person_receipt(bk: str, led, action: str, how: str, args, ref) -> None:
    """Every successful person action writes a host-private receipt (8c ruling; spec 4.0 "a person-path
    command on the owner host writes its own receipt"), on either path, so the driver can tell it from tamper:
    kind `person`, record_id person_record_id(action, ref), rev 1, decision {action, target: job}, action_id
    `<how>:<action>` (`requester:halt`, `tty:deactivate`, ...), the ledger's generation and incarnation,
    args_sha of the submitted argv. receipts.find(bk, job, "person", record_id, rev=1, ...) then
    receipts.matches(r, {"kind": "person", "id": record_id, "rev": 1, "action": action, "target": job}). A
    failure is reported, not fatal (the action is done)."""
    import time
    from swarm import drivegate, receipts
    try:
        receipts.write(bk, receipts.Receipt(
            board_key=bk, job=led.job, generation=led.generation, incarnation=led.incarnation,
            action_id=f"{how}:{action.replace(' ', '_')}", attempt=0, launch=None, kind="person",
            record_id=person_record_id(action, ref), rev=1,
            content_sha=receipts.content_sha("person", {"action": action, "target": led.job}),
            args_sha=drivegate.grant_id(args, person.host_pid()), expires_at=time.time() + PERSON_RECEIPT_DAYS * 86400))
    except (OSError, ValueError) as exc:
        print(f"warning: the person receipt of this {action} was not written: {exc}", file=sys.stderr)


def core_halt(board, led, by: str, reason) -> None:
    """The ledger side of a halt (no save)."""
    if led.terminal is not None:
        raise Refuse(f"{led.job} is finished; nothing to halt")
    if led.halted is not None:
        raise Refuse(f"{led.job} is already halted")
    halt = {"by": by, "at": _now_iso(board)}
    if (reason or "").strip():
        halt["reason"] = reason.strip()[:4000]
    led.halted = halt


def core_unhalt(board, led) -> None:
    """The ledger side of an unhalt (no save): a repaired ledger wants budgets first; the halted time is
    added to halted_total_seconds (deadlines shift, [F5])."""
    if led.halted is None:
        raise Refuse(f"{led.job} is not halted")
    if str(led.halted.get("reason", "")).startswith(ledger.REPAIR_REASON) and not led.budgets:
        raise Refuse(f"{led.job}'s ledger was rebuilt by a drive repair and has no budgets: set them first, net "
                     f"of the spend the repair reported (swarm drive budget --job {led.job} KEY=VALUE ..., or "
                     "re-activate with --budget), then unhalt")
    since = _dt.datetime.fromisoformat(led.halted["at"])
    if since.tzinfo is None:
        since = since.replace(tzinfo=_dt.timezone.utc)
    led.halted_total_seconds += max(0.0, (board.now() - since).total_seconds())
    led.halted = None


def cmd_halt(board, cfg, args) -> int:
    bk = board_key(cfg)
    led = _drive_ledger(bk, args.job)
    if person.codex_session(os.environ):
        return control_request(board, cfg, args)
    who = person.require("halt", args.job, board_key=bk, env=os.environ, who=_caller(bk, args.job, led, args))
    led = _drive_ledger(bk, args.job)
    core_halt(board, led, f"person ({who})", args.reason)
    led.save()
    person_receipt(bk, led, "halt", who, args, led.ledger_rev)
    return _record("halt", led.ledger_rev, 1)


def cmd_unhalt(board, cfg, args) -> int:
    bk = board_key(cfg)
    led = _drive_ledger(bk, args.job)
    if person.codex_session(os.environ):
        return control_request(board, cfg, args)
    who = person.require("unhalt", args.job, board_key=bk, env=os.environ, who=_caller(bk, args.job, led, args))
    led = _drive_ledger(bk, args.job)
    core_unhalt(board, led)
    led.save()
    person_receipt(bk, led, "unhalt", who, args, led.ledger_rev)
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


def core_repair(board, bk: str, job: str, by: str, generation):
    """ledger.repair with the board's claims (drive.enabled, a closed job): the Repaired result; Refuse."""
    data = board.job_data(job) or {}
    try:
        return ledger.repair(bk, job, at=_now_iso(board), by=by, generation=generation,
                             claimed=data.get("drive.enabled") == "1", board_finished=_board_finished(board, job))
    except ledger.LedgerExistsError as exc:
        raise Refuse(f"{exc}: run the repair again")
    except ledger.LedgerBadError as exc:   # e.g. no rename relative to a directory (Windows: no drive mode)
        raise Refuse(f"drive repair cannot run here: {exc}")
    except ValueError as exc:
        raise Refuse(str(exc))


def cmd_drive_repair(board, cfg, args) -> int:
    """Person path (spec F2): a drive ledger that does not read is moved aside (never deleted) and a fresh one
    takes its place, under one lock (ledger.repair). The generation comes from the archived ledger, else from a
    person's --generation, never from the board. A finished job stays finished; otherwise the fresh ledger is
    halted and unhalt wants budgets set first. Everything not recovered is listed on stderr."""
    bk = board_key(cfg)
    try:
        current = ledger.load(bk, args.job)
    except ledger.LedgerBadError:
        current = None   # the hook's grant then names the owner record's requester (drivegate._grant_owner)
    if person.codex_session(os.environ):
        return control_request(board, cfg, args)
    who = person.require("drive repair", args.job, board_key=bk, env=os.environ,
                         who=_caller(bk, args.job, current, args))
    data = board.job_data(args.job) or {}
    rep = core_repair(board, bk, args.job, f"person ({who})", args.generation)
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
    person_receipt(bk, rep.ledger, "drive repair", who, args, rep.ledger.ledger_rev)
    return _record("halt", rep.ledger.ledger_rev, 1)


# --- control requests (a Codex session's ledger-writing commands; Task 6b round 3, coordinator ruling) ----

def control_request(board, cfg, args) -> int:
    """A Codex session's ledger-writing control command: the CLI writes no ledger (its sandbox cannot, and
    the caller cannot be known inside it). It posts `CONTROL-REQUEST <kind> <args sha> <fields>` and prints
    the record line; the PostToolUse hook receipts it from outside the sandbox, and the driver applies it
    (apply_control_request) in its pass, ledger first, after checking the receipt, the arguments, the
    caller's authority, expiry and one-time use, and posts CONTROL-APPLIED or CONTROL-REFUSED."""
    from swarm import drive_control
    text = drive_control.request_text(args)
    if text is None:
        raise Refuse("this command has no control request")
    if len(text) > board.message_cap():
        raise Refuse("the request is longer than a board message: send fewer arguments at a time")
    res = board.post(args.job, drive_control.REQUESTER_NAME, text, None)
    print(f"requested: the supervisor applies it in its next pass if you may (CONTROL-APPLIED {res.id} on the "
          f"board), or posts CONTROL-REFUSED {res.id} with the reason")
    return _record("control_request", res.id, 1)


def find_control_requests(board, bk: str, job: str, led, now, after: int = 0) -> list:
    """The receipted CONTROL-REQUESTs of `job` after message id `after` (see drive_control.find)."""
    from swarm import drive_control
    return drive_control.find(board, bk, job, led, now, after=after)


def apply_control_request(board, cfg: dict, led, req, now) -> tuple:
    """The driver's side (8c calls it in its pass): apply one ControlRequest (from find_control_requests) to
    the ledger `led` (None only for a `repair` of a ledger that does not read). `now` is an aware datetime.
    Returns (applied: bool, why: str). See drive_control.apply."""
    from swarm import drive_control
    return drive_control.apply(board, cfg, led, req, now)


# --- swarm drive run ([T21]) ---------------------------------------------------------------------

def cmd_drive_run(cfg, args) -> int:
    """`swarm drive run [--job J]` (spec 3.11 [T21]): the session driver's loop in the foreground, for progress
    with no session open: every unfinished drive job of this board (or J), until none is left or the kill switch.
    Installs nothing and never calls a model. One per board (and job filter) at a time."""
    from swarm.supervisor import session_driver
    bk = board_key(cfg)
    why = session_driver.launch_caller(os.getpid())       # its env, its own environment and every ancestor
    if why:
        print(f"swarm drive run: refused: {why}", file=sys.stderr)
        return 1
    try:   # a person's command (review rr1): never under an agent host, and confirmed on the tty
        person.require("drive run", args.job or "every drive job", board_key=bk, who=None, env=os.environ)
    except person.PersonRefused as exc:
        print(f"swarm drive run: {exc}", file=sys.stderr)
        return 1
    if not session_driver.session_jobs(bk, None, args.job):
        print(f"swarm drive run: no unfinished drive job{' ' + args.job if args.job else ''} on this board")
        return 0
    lock = session_driver.run_lock_name(bk, args.job)
    print(f"swarm drive run: driving {args.job or 'every drive job of this board'} (Ctrl-C stops; nothing is "
          "installed)", flush=True)
    try:
        why = session_driver.serve(cfg, session=None, job=args.job, watch=None,
                                   config_path=str(getattr(args, "config", "") or ""), lock=lock,
                                   say=lambda line: print(line, flush=True))
    except KeyboardInterrupt:
        print("swarm drive run: stopped")
        return 130
    if why == "already running":
        print("swarm drive run: another `swarm drive run` drives these jobs already", file=sys.stderr)
        return 1
    print(f"swarm drive run: done ({why})")
    return 0


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


def _with_record(fn):
    """A post/done handler that ends, in a drive job, with the record line of the one message it posted
    (`kind=done|post id=<message id>`; post only with --to): the board it runs on remembers what it posted."""
    if getattr(fn, "_drive_record", False):
        return fn

    def handler(board, cfg, args):
        posted = []

        class Recording:
            def __getattr__(self, name):
                return getattr(board, name)

            def post(self, *a, **k):
                row = board.post(*a, **k)
                posted.append(row)
                return row
        if args.cmd == "done" and not (args.artifact or args.branch):
            try:
                led = ledger.load(board_key(cfg), args.job)
            except (ledger.LedgerBadError, OSError):
                led = None
            name = args.name or board.active_agent_name(getattr(args, "key", None))
            if led and any(agent.role == "engineering_lead" and agent.name == name
                           for agent in board.agents(args.job, include_departed=False)):
                print("warning: a hand-off meant for the judge needs --artifact <BRANCH>@<FULL SHA>",
                      file=sys.stderr)
        rc = fn(Recording(), cfg, args)
        if not rc and len(posted) == 1 and (args.cmd == "done" or getattr(args, "to", None)):
            drive_record(cfg, args.job, "done" if args.cmd == "done" else "post", posted[0].id)
        return rc
    handler._drive_record = True
    return handler


def _closed(cfg: dict, job: str) -> bool:
    try:
        from swarm.board import open_board
        with open_board(cfg) as board:
            js = board.job_status(job)
        return js is not None and js.status != "active"
    except Exception:
        return False


def _with_person_receipt(fn):
    """`swarm deactivate` of a drive job: a person's (the requester's grant, or a confirmed tty) writes its
    person receipt once the job is closed, and every closing deactivation ends with `kind=deactivate`."""
    if getattr(fn, "_drive_record", False):
        return fn

    def handler(cfg, args):
        led, how = deactivate_person(cfg, args)
        args._person_how = how     # a person's --no-learn: the grant this command already used up
        rc = fn(cfg, args)
        if not rc and _closed(cfg, args.job):
            deactivate_receipt(cfg, args, led, how)
            deactivate_record(cfg, args.job)
            _clear_drive_claim(cfg, args.job)   # [RR1a]/6a: a closed job is not driven
        return rc
    handler._drive_record = True
    return handler


def _clear_drive_claim(cfg: dict, job: str) -> None:
    """Clear the board's drive claim of a job `swarm deactivate` closed (drive_activate.clear_claim). Never raises:
    the driver's ledger-first terminalization clears it too."""
    try:
        from swarm import drive_activate
        from swarm.board import open_board
        with open_board(cfg) as board:
            drive_activate.clear_claim(board, job)
    except Exception:
        pass


def register(sub, handlers: dict, commands: dict | None = None) -> None:
    """Add the control parsers to the subparsers `sub` and their handlers to `handlers` (cli's
    BOARD_COMMANDS); wrap post and done (handlers) and deactivate (`commands`, cli's COMMANDS) so a drive
    job's record line and person receipt follow them. Called from cli._parser (repeat calls wrap once)."""
    for name in ("post", "done"):
        if name in handlers:
            handlers[name] = _with_record(handlers[name])
    if commands is not None and "deactivate" in commands:
        commands["deactivate"] = _with_person_receipt(commands["deactivate"])
    h = sub.add_parser("halt", help="a person stops a drive job on purpose (until unhalt or cancel)")
    h.add_argument("--job", required=True); h.add_argument("--reason")
    u = sub.add_parser("unhalt", help="a person resumes a halted drive job")
    u.add_argument("--job", required=True)
    rq = sub.add_parser("run", help="queue or re-queue a named launch")
    rq.add_argument("--job", required=True); rq.add_argument("--name", required=True)
    rq.add_argument("--role", required=True); rq.add_argument("--brief", required=True)
    rq.add_argument("--model"); rq.add_argument("--harness", default="claude", choices=("claude", "codex"))
    rq.add_argument("--workdir")
    rp = drive_subparsers(sub).add_parser(
        "repair", help="move a bad drive ledger aside; a fresh one starts halted (a person)")
    rp.add_argument("--job", required=True)
    rp.add_argument("--generation", type=int, metavar="N",
                    help="the job's generation, when the bad ledger no longer tells it (never read from the board)")
    add_drive_command("repair", cmd_drive_repair)
    rn = drive_subparsers(sub).add_parser(
        "run", help="drive this board's drive jobs in the foreground until none is left (no session needed; "
                    "installs nothing)")
    rn.add_argument("--job", help="only this job")
    add_drive_command("run", cmd_drive_run, board=False)
    rv = sub.add_parser("review", help="post a structured review of an artifact (an enrolled reviewer)")
    rv.add_argument("--job", required=True); rv.add_argument("--artifact", required=True)
    rv.add_argument("outcome", choices=REVIEW_OUTCOMES, type=str.lower)
    rg = rv.add_mutually_exclusive_group(required=True)
    rg.add_argument("--report-text", metavar="TEXT", help="the review report itself (one literal argument)")
    rg.add_argument("--report", metavar="FILE", help="a file holding the review report")
    rv.add_argument("--key", help="the reviewing agent's key")
    # A jailed launch's broker (Task 20): the report's content, in a file only the supervisor writes; --report is
    # then the path as the launch typed it (posted, not read here)
    rv.add_argument("--report-content", help=argparse.SUPPRESS)
    gr = sub.add_parser("goal-ready", help="declare the goal ready for the judge (the lead or a person)")
    gr.add_argument("--job", required=True); gr.add_argument("--candidate", required=True)
    gr.add_argument("--add-requirements", action="append", metavar="PATH")
    # A jailed launch's broker (Task 20): "PATH\nFILE": the requirement PATH in the launch's work tree (the broker
    # reached it without following a link), pinned at the content the broker read into FILE
    gr.add_argument("--add-requirements-pinned", action="append", help=argparse.SUPPRESS)
    handlers.update({"halt": _run(cmd_halt), "unhalt": _run(cmd_unhalt), "run": _run(cmd_run),
                     "review": _run(cmd_review), "goal-ready": _run(cmd_goal_ready),
                     "drive": _run(cmd_drive)})


# --- deactivate (cli.cmd_deactivate calls these for a drive job) -------------------------------------------

def deactivate_person(cfg: dict, args) -> tuple:
    """Before a drive job is deactivated: (ledger, how) when the caller is a person (the requester's grant,
    or a confirmed tty outside any agent host), else (ledger or None, None): the deactivation goes ahead as
    before, but without a person receipt the driver treats it as tamper (spec 3.2). Never raises."""
    try:
        bk = board_key(cfg)
        led = ledger.load(bk, args.job)
        if led is None:
            return None, None
        person.forget()
        how = person.require("deactivate", args.job, board_key=bk, env=os.environ,
                             who=person.caller(bk, args.job, led, args))
        return led, how
    except Exception as exc:
        print(f"note: {exc}" if isinstance(exc, person.PersonRefused) else f"note: no person receipt: {exc}",
              file=sys.stderr)
        return None, None


def deactivate_receipt(cfg: dict, args, led, how) -> None:
    """After a successful deactivation of a drive job by a person: its person receipt (record id
    `deactivate:<generation>`), which tells the driver this close is a cancel."""
    if led is not None and how:
        person_receipt(board_key(cfg), led, "deactivate", how, args, led.generation)


def deactivate_record(cfg: dict, job: str) -> None:
    """The last line of a deactivation of a drive job: `swarm-record: kind=deactivate id=<generation> rev=1`
    (drivegate receipts it for the requester session). Nothing for other jobs. Never raises."""
    try:
        led = ledger.load(board_key(cfg), job)
    except Exception:
        return
    if led is not None:
        _record("deactivate", led.generation, 1)
