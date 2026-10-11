"""Host-private authorization of drive-job control records (spec 3.4, 4.0, 4.5).

A seat row alone never launches anything: the driver acts on a seat request only when a receipt
(swarm.receipts, written by the unsandboxed PostToolUse hook from the command's own output) binds
the row's id and revision and the canonical fields of the row. Board writers are untrusted, so the
comparison is always against the row as it is now, never against what the board says it was.
"""
from __future__ import annotations

import hashlib
import time

from swarm import driveroles, receipts
from swarm.supervisor import ledger

# Record kinds the CLI prints in its last line (`swarm-record: kind=<k> id=<n> rev=<r>`) mapped to
# the receipt kind the driver checks (receipts.KINDS). Person-path and requester-session commands
# all count as `person`, with the command itself in the decision fields.
RECORD_RECEIPT_KIND = {
    "done": "done",
    "review": "review",
    "verdict": "verdict",
    "seat": "seat_request",
    "ask": "ask",
    "answer": "answer",
    "post": "post",
    "goal_ready": "goal_ready",
    "ack": "ack",
    "withdraw": "withdraw",
    "halt": "person",
    "unhalt": "person",
    "deactivate": "person",
    "activate": "person",
}


def receipt_kind(record_kind: str) -> str | None:
    """The receipt kind for a record-format kind, None for an unknown one."""
    return RECORD_RECEIPT_KIND.get(record_kind)


def canonical_seat_args(seat) -> list[str]:
    """The argv `swarm seat request` needs to produce the fields of `seat` (a board Seat row), in a
    fixed order. Optional fields are emitted only when set. The receipt's args_sha is the hash of
    this list, so a row changed after the hook's receipt no longer matches."""
    argv = ["seat", "request", "--job", seat.job, "--seat", seat.seat, "--role", seat.role,
            "--harness", seat.harness]
    if seat.model is not None:
        argv += ["--model", seat.model]
    argv += ["--brief", seat.brief_path, "--workdir", seat.workdir]
    if seat.title is not None:
        argv += ["--title", seat.title]
    if seat.task_ref is not None:
        argv += ["--task", seat.task_ref]
    argv += ["--complexity", seat.complexity]
    return argv


def seat_decision(seat) -> dict:
    """The row as receipts.matches() reads it: kind, id, rev and the seat_request decision fields."""
    return {"kind": "seat_request", "id": str(seat.id), "rev": seat.revision, "role": seat.role,
            "model": seat.model, "brief": seat.brief_path, "workdir": seat.workdir}


def requirements_digest(pins: dict) -> str:
    """The digest of pinned requirements (path -> blob id) a `GOAL-READY <candidate> req=<digest>` carries:
    sha256 of `path@blob` lines in path order. The ledger's pinned_requirements is the one source of it."""
    return hashlib.sha256("".join(f"{p}@{b}\n" for p, b in sorted(pins.items())).encode()).hexdigest()


def _argv_of(row) -> str:
    return receipts.args_sha(canonical_seat_args(row))


def _row_receipt(board_key: str, led, row, *, now: float, any_rev: bool):
    """The seat_request receipt for `row` (of this ledger's incarnation and the row's generation) whose argv
    hash and decision fields match the row as it is now, or None. `any_rev` accepts the receipt of an earlier
    revision of the row (the driver carries an authorization to each new revision, a moment later): the
    decision fields, which include the role, must still be the receipted ones."""
    r = receipts.find_latest(board_key, led.job, "seat_request", str(row.id), incarnation=led.incarnation,
                             generation=row.generation, now=now)
    if r is None or r.args_sha != _argv_of(row):
        return None
    if not any_rev and r.rev != row.revision:
        return None
    decision = dict(seat_decision(row), rev=r.rev)
    return r if receipts.matches(r, decision) else None


def receipted_role(board_key: str, led, row, *, now: float | None = None) -> str | None:
    """The role of a seat row as the host authorized it: the row's role when a seat_request receipt of
    this incarnation matches the row's decision fields (role, model, brief, workdir) and argv, else None.
    A board writer who rewrites a role after the receipt gets None: callers treat that as the strictest
    case, never as the role the row now says."""
    row_now = time.time() if now is None else now
    return row.role if _row_receipt(board_key, led, row, now=row_now, any_rev=True) is not None else None


def _origin_ok(board_key: str, board, led, r, seat: str, now: float) -> bool:
    """Who wrote the receipt: the requester session (`requester:seat`), the driver for its own seats
    (`driver|authorize|<seat>`), or the job's lead seat in an open driver action of its own (the intent
    named by the receipt exists, belongs to that seat, and the seat is a lead whose own row is receipted
    as engineering_lead). Anything else was not written by a path that may authorize a seat."""
    if r.action_id == "requester:seat":
        return r.attempt == 0 and r.seat is None
    if r.action_id == f"driver|authorize|{seat}":
        return r.attempt == 0 and r.seat == seat
    if not r.seat or not driveroles.seat_name_is_lead(r.seat):
        return False
    if not any(i.action_id == r.action_id and i.attempt == r.attempt and i.seat == r.seat for i in led.intents):
        return False
    lead = board.seat(led.job, led.generation, r.seat)
    return (lead is not None and _row_receipt(board_key, led, lead, now=now, any_rev=True) is not None
            and lead.role == driveroles.LEAD_ROLE)


def seat_request_authorized(board_key: str, board, job: str, generation: int, seat: str, *,
                            now: float | None = None) -> bool:
    """Whether the current row of `seat` in `job`/`generation` is one the host authorized: a
    seat_request receipt for the row's id at the row's revision, of this job's incarnation and
    generation, written by a path that may authorize a seat (the requester session, the driver, or the
    job's lead in an action of its own: _origin_ok), whose args_sha is the canonical argv of the row now
    and whose decision fields still match the row (receipts.matches). False for anything missing,
    unreadable or changed."""
    row = board.seat(job, generation, seat)
    if row is None:
        return False
    led = ledger.load(board_key, job)
    if led is None or not led.incarnation:
        return False
    at = time.time() if now is None else now
    r = _row_receipt(board_key, led, row, now=at, any_rev=False)
    return r is not None and _origin_ok(board_key, board, led, r, seat, at)
