"""Control requests: a Codex session's ledger-writing control commands (Task 6b round 3, coordinator ruling;
the LAND-REQUEST pattern of Task 11b).

A Codex seat's sandbox can neither write the drive ledger nor use a grant, so the CLI cannot know who calls
it and must not write the ledger. Instead (drive_cli.control_request):

1. the CLI posts `CONTROL-REQUEST <kind> <args sha>` (args sha: receipts.args_sha of the submitted argv of
   its parsed arguments) and prints `swarm-record: kind=control_request id=<message id> rev=1`;
2. the unsandboxed PostToolUse hook (drivegate.control_request_receipt) parses the command it saw, checks
   that the message is exactly the request that command makes, and writes a `control_request` receipt naming
   the caller from the same host-private contexts as every receipt (the job's requester main session, or the
   seat the marker, run file and ledger intent name; never a subagent), and next to it the request's
   arguments (store_body: a host-private file, so a board message's length never limits them and a sandboxed
   seat cannot write them);
3. the driver, in its pass, is the only writer: find() lists the receipted requests, apply() checks them
   again (the receipt, its decision fields, the message, the args digest, the generation and incarnation,
   expiry, the caller's authority under the same rules as the Claude path) and applies the change ledger
   first, then the board side, and posts CONTROL-APPLIED <id> <kind> or CONTROL-REFUSED <id> <why>. A
   decided request is used up like a grant: its receipt and stored arguments are removed (_consume), and the
   ledger's `ctlreq:<id>` mark is the second check.

Kinds: halt, unhalt, repair, cancel (the requester); ack (the live lead); pins, withdraw, release (the live
lead or the requester). A Claude session keeps the direct path (a grant)."""
from __future__ import annotations

import json
from dataclasses import dataclass
from types import SimpleNamespace

PREFIX = "CONTROL-REQUEST "
APPLIED = "CONTROL-APPLIED "
REFUSED = "CONTROL-REFUSED "
REQUESTER_NAME = "codex session"        # the author of a request post (a label: the receipt names the caller)
SUPERVISOR_NAME = "swarm supervisor"    # the author of the driver's answers (as land.py's)
MARK = "ctlreq:"                        # ledger watermark ctlreq:<message id> = 1: that request was decided
FLOOR = "ctlreq-floor"                  # every request id at or below it counts as decided
MARKS_KEEP = 64                         # decided ids kept above the floor (the rest raise the floor)
KIND = "control_request"                # the receipt kind
REQUESTER_KINDS = ("halt", "unhalt", "repair", "cancel")
LEAD_KINDS = ("ack",)
LEAD_OR_REQUESTER_KINDS = ("pins", "withdraw", "release")
_SKIP = ("config", "no_color", "func")


def kind_of(ns) -> str | None:
    """The request kind of a parsed command, or None for a command that has no control request."""
    cmd = getattr(ns, "cmd", None)
    if cmd in ("halt", "unhalt"):
        return cmd
    if cmd == "goal-ready":
        return "pins"
    if cmd == "drive" and getattr(ns, "dcmd", None) == "repair":
        return "repair"
    if cmd == "seat":
        scmd = getattr(ns, "scmd", None)
        if scmd == "ack":
            return "ack"
        if scmd == "cancel":
            return "cancel"
        if scmd == "release":
            return "withdraw" if getattr(ns, "withdraw_artifact", None) else "release"
    return None


def fields_of(ns) -> dict:
    """The parsed arguments a request carries: every JSON value of the namespace but the config path."""
    out = {}
    for k, v in sorted(vars(ns).items()):
        if k in _SKIP or callable(v):
            continue
        if v is None or isinstance(v, (str, int, bool)) or (isinstance(v, list) and all(
                isinstance(x, (str, int)) for x in v)):
            out[k] = v
    return out


def args_sha(fields: dict) -> str:
    from swarm import drivegate, receipts
    return receipts.args_sha(drivegate.submitted_argv(SimpleNamespace(**fields)))


def text_of(kind: str, fields: dict) -> str:
    return f"{PREFIX}{kind} {args_sha(fields)}"


def request_text(ns) -> str | None:
    """The CONTROL-REQUEST message of a parsed command (the CLI posts it; the hook checks it), or None."""
    kind = kind_of(ns)
    return text_of(kind, fields_of(ns)) if kind else None


def parse_text(text: str):
    """(kind, args sha) of a CONTROL-REQUEST message, else None."""
    if not isinstance(text, str) or not text.startswith(PREFIX):
        return None
    parts = text[len(PREFIX):].split(" ")
    if len(parts) != 2 or len(parts[1]) != 64 or any(c not in "0123456789abcdef" for c in parts[1]):
        return None
    if parts[0] not in REQUESTER_KINDS + LEAD_KINDS + LEAD_OR_REQUESTER_KINDS:
        return None
    return parts[0], parts[1]


def _body_name(bk: str, job: str, msg_id: int) -> str:
    import hashlib
    return "ctlbody-" + hashlib.sha256("\0".join((bk, job, str(msg_id))).encode("utf-8", "surrogatepass")).hexdigest() + ".json"


BODY_MAX = 128 * 1024


def store_body(bk: str, job: str, msg_id: int, fields: dict) -> None:
    """The hook's copy of a request's arguments, host-private next to its receipt (enrolled directory)."""
    from swarm import enrolment, safefs
    with enrolment._dir(create=True) as d:
        safefs.write_atomic(d, _body_name(bk, job, msg_id), json.dumps(fields, sort_keys=True), durable=True)


def load_body(bk: str, job: str, msg_id: int, kind: str, sha: str):
    """The stored arguments of request `msg_id` if they are the receipted ones (their kind, job and digest),
    else None."""
    from swarm import enrolment, safefs
    try:
        with enrolment._dir(create=False) as d:
            data = safefs.read(d, _body_name(bk, job, msg_id), BODY_MAX)
        fields = json.loads(data) if data is not None else None
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(fields, dict) or fields.get("job") != job:
        return None
    try:
        if kind_of(SimpleNamespace(**fields)) != kind or args_sha(fields) != sha:
            return None
    except (TypeError, ValueError):
        return None
    return fields


@dataclass(frozen=True)
class ControlRequest:
    """A receipted CONTROL-REQUEST: its message id, kind, args digest and fields, and the caller the receipt
    names (seat None and an action id `requester:...` for the requester session)."""
    id: int
    job: str
    kind: str
    args_sha: str
    fields: dict
    seat: str | None
    action_id: str
    generation: int


def _epoch(now) -> float:
    return now.timestamp() if hasattr(now, "timestamp") else float(now)


def _identity(bk: str, job: str, led):
    from swarm import receipts
    return ((led.incarnation, led.generation) if led is not None
            else (receipts.GRANT_INCARNATION_NO_LEDGER, 1))


def _receipt(bk: str, job: str, led, msg_id: int, kind: str, sha: str, now):
    """The control_request receipt of message `msg_id` matching (kind, sha), or None."""
    from swarm import receipts
    inc, gen = _identity(bk, job, led)
    r = receipts.find(bk, job, KIND, str(msg_id), rev=1, incarnation=inc, generation=gen, now=_epoch(now))
    row = {"kind": KIND, "id": str(msg_id), "rev": 1, "request": kind, "args_sha": sha}
    return r if r is not None and receipts.matches(r, row) else None


def find(board, bk: str, job: str, led, now, after: int = 0) -> list:
    """The CONTROL-REQUEST messages of `job` after id `after` that carry a matching receipt of this ledger's
    incarnation and generation (for `led` None: a repair, the no-ledger identity), oldest first. A message
    without one is not a request: the board is writable by agents. Requests already decided are left out."""
    out = []
    for msg in board.messages_after(after, job):
        parsed = parse_text(msg.message)
        if parsed is None or (led is not None and f"{MARK}{msg.id}" in led.watermarks):
            continue
        kind, sha = parsed
        if led is not None and decided(led, msg.id):
            # at or below the floor but never decided (its receipt came after MARKS_KEEP later decisions):
            # tell the caller once, and use it up, so it asks again instead of waiting for nothing
            if _receipt(bk, job, led, msg.id, kind, sha, now) is not None:
                _consume(bk, job, msg.id)
                _post(board, job, f"{REFUSED}{msg.id} expired-below-floor")
            continue
        if (led is None) != (kind == "repair"):
            continue
        r = _receipt(bk, job, led, msg.id, kind, sha, now)
        fields = load_body(bk, job, msg.id, kind, sha) if r is not None else None
        if fields is None:
            continue
        out.append(ControlRequest(msg.id, job, kind, sha, fields, r.seat, r.action_id, r.generation))
    return out


class _Refused(Exception):
    pass


def _authorize(board, led, req: ControlRequest) -> str:
    """Who the request may act as ("requester" or "lead <seat>"), under the Claude path's rules; _Refused."""
    from swarm.drive_cli import _is_lead_seat
    from swarm.board.base import SEAT_CLOSED
    requester = req.seat is None and req.action_id.startswith("requester:")
    lead = None
    if req.seat and _is_lead_seat(req.seat) and led is not None:
        row = board.seat(led.job, led.generation, req.seat)
        lead = req.seat if row is not None and row.state not in SEAT_CLOSED else None
    if req.kind in REQUESTER_KINDS:
        if not requester:
            raise _Refused(f"{req.kind} is a person's action: only the job's requester session may request it")
        return "requester"
    if req.kind in LEAD_KINDS:
        if lead is None:
            raise _Refused(f"{req.kind} comes from the job's live lead seat")
        return f"lead {lead}"
    if req.kind in LEAD_OR_REQUESTER_KINDS:
        if lead is None and not requester:
            raise _Refused(f"{req.kind} comes from the job's live lead seat or its requester session")
        return f"lead {lead}" if lead else "requester"
    raise _Refused(f"unknown request kind {req.kind!r:.40}")


def _post(board, job: str, text: str) -> None:
    try:
        board.post(job, SUPERVISOR_NAME, text[:board.message_cap()], None)
    except Exception:
        pass


def apply(board, cfg: dict, led, req: ControlRequest, now) -> tuple:
    """Apply one request (see the module doc). Returns (applied, why). A request that fails a check of its
    own (authority, arguments, state) is decided for good: its `ctlreq:<id>` mark is saved with the refusal,
    so it is never tried again, and CONTROL-REFUSED is posted. A request whose receipt, message or digest does
    not verify, or that was decided already, is refused without a mark (nothing it says is trusted)."""
    from swarm import drive_cli
    from swarm.supervisor import ledger as ledger_mod
    bk = drive_cli.board_key(cfg)
    job = req.job
    if led is not None and led.job != job:
        return False, "another job's request"
    if led is not None and decided(led, req.id):
        _consume(bk, job, req.id)                    # decided before: what is left of it goes
        return False, "already decided (a request is used once)"
    msg = next((m for m in board.messages_after(req.id - 1, job) if m.id == req.id), None)
    parsed = parse_text(msg.message) if msg is not None else None
    if parsed is None or parsed != (req.kind, req.args_sha):
        return False, "the request message is not on the board as it was receipted"
    if load_body(bk, job, req.id, req.kind, req.args_sha) != req.fields:
        return False, "no matching receipt (the request's arguments are not the receipted ones)"
    r = _receipt(bk, job, led, req.id, req.kind, req.args_sha, now)
    if r is None or (r.seat, r.action_id) != (req.seat, req.action_id):
        return False, "no matching receipt (a request counts only when the caller's hook receipted it)"
    if (led is None) != (req.kind == "repair"):
        return False, "a repair needs an unreadable ledger; every other request a readable one"
    f = SimpleNamespace(**req.fields)
    try:
        who = _authorize(board, led, req)
        by = f"{who} (control request {req.id})"
        if req.kind == "repair":
            rep = drive_cli.core_repair(board, bk, job, f"person ({by})", getattr(f, "generation", None))
            _consume(bk, job, req.id)                # one repair per request, however often the ledger goes bad
            _mark(bk, job, req.id)
            _post(board, job, f"{APPLIED}{req.id} repair: {', '.join(rep.lost)[:300] or 'nothing lost'}")
            return True, "repaired"
        row = None
        if req.kind == "halt":
            drive_cli.core_halt(board, led, f"person ({by})", getattr(f, "reason", None))
        elif req.kind == "unhalt":
            drive_cli.core_unhalt(board, led)
        elif req.kind == "ack":
            drive_cli.core_ack(board, job, led, who.split(" ", 1)[1], int(f.upto), list(getattr(f, "event", None) or []))
        elif req.kind == "pins":
            drive_cli.core_pins(bk, job, led, list(getattr(f, "add_requirements", None) or []))
        elif req.kind == "withdraw":
            group, reason = drive_cli.check_withdraw_args(f.withdraw_artifact, getattr(f, "reason", None))
            row = drive_cli.core_withdraw(board, job, led, f.seat, group, reason)
        elif req.kind in ("release", "cancel"):
            row = drive_cli._open_seat_row(board, job, led, f.seat)
    except (_Refused, drive_cli.Refuse) as exc:
        why = str(exc).removeprefix("refused: ")
        if led is not None:
            _decide(ledger_mod.load(bk, job) or led, req)   # a fresh copy: the core may have changed `led`
        _consume(bk, job, req.id)                    # decided: refused for good
        _post(board, job, f"{REFUSED}{req.id} {why}")
        return False, why
    remember(led, req.id)                            # used once: saved with the change itself
    try:
        led.save()                                   # ledger first
    except ledger_mod.LedgerConflict:
        return False, "the ledger changed meanwhile: the next pass tries again"
    _consume(bk, job, req.id)                        # decided: the receipt and its arguments are used up
    if row is not None:
        try:
            drive_cli._set_state(board, row, "cancelled" if req.kind == "cancel" else "released", job)
        except drive_cli.Refuse as exc:
            _post(board, job, f"{APPLIED}{req.id} {req.kind} (the seat row: {exc})")
            return True, str(exc)
    if req.kind in ("halt", "unhalt", "cancel"):
        drive_cli.person_receipt(bk, led, {"cancel": "seat cancel"}.get(req.kind, req.kind), "requester",
                                 SimpleNamespace(**req.fields), led.ledger_rev if row is None else row.id)
    _post(board, job, f"{APPLIED}{req.id} {req.kind}")
    return True, req.kind


def decided(led, msg_id: int) -> bool:
    """Whether request `msg_id` was decided: at or below the floor, or one of the recent marks."""
    return msg_id <= led.watermarks.get(FLOOR, 0) or f"{MARK}{msg_id}" in led.watermarks


def remember(led, msg_id: int) -> None:
    """Mark request `msg_id` decided in `led` (not saved), keeping the marks bounded (CX1 X3): at most
    MARKS_KEEP ids above the floor; older ones raise the floor. Message ids only grow, so a request at or
    below the floor that was never decided can only be one older than MARKS_KEEP later decisions (its
    receipt was late): it is not applied and has to be asked again."""
    wm = led.watermarks
    wm[f"{MARK}{msg_id}"] = 1
    ids = sorted(int(k[len(MARK):]) for k in wm if k.startswith(MARK))
    if len(ids) > MARKS_KEEP:
        drop = ids[:len(ids) - MARKS_KEEP]
        for i in drop:
            del wm[f"{MARK}{i}"]
        wm[FLOOR] = max(wm.get(FLOOR, 0), drop[-1])


def _consume(bk: str, job: str, msg_id: int) -> None:
    """Use up a decided request, as a grant is used up: its receipt and its stored arguments are removed, so
    nothing can apply it again even if the ledger that marks it decided is lost (a repair that does not keep
    the watermarks, a ledger that goes bad again). Never raises."""
    from swarm import enrolment, receipts, safefs
    receipts.drop(bk, job, KIND, str(msg_id))
    try:
        with enrolment._dir(create=False) as d:
            safefs.unlink(d, _body_name(bk, job, msg_id))
    except OSError:
        pass


def _decide(led, req: ControlRequest) -> None:
    """Save a refused request's mark, so it is never tried again (best effort: the next pass retries)."""
    from swarm.supervisor import ledger as ledger_mod
    remember(led, req.id)
    try:
        led.save()
    except (ledger_mod.LedgerConflict, ledger_mod.LedgerTooLargeError, OSError):
        pass


def _mark(bk: str, job: str, msg_id: int) -> None:
    from swarm.supervisor import ledger as ledger_mod
    try:
        led = ledger_mod.load(bk, job)
        if led is not None:
            remember(led, msg_id)
            led.save()
    except (ledger_mod.LedgerBadError, ledger_mod.LedgerConflict, OSError):
        pass
