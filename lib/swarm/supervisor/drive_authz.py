"""Host-private authorization of drive-job control records (spec 3.4, 4.0, 4.5).

A seat row alone never launches anything: the driver acts on a seat request only when a receipt
(swarm.receipts, written by the unsandboxed PostToolUse hook from the command's own output) binds
the row's id and revision and the canonical fields of the row. Board writers are untrusted, so the
comparison is always against the row as it is now, never against what the board says it was.
"""
from __future__ import annotations

import time

from swarm import receipts
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


def seat_request_authorized(board_key: str, board, job: str, generation: int, seat: str, *,
                            now: float | None = None) -> bool:
    """Whether the current row of `seat` in `job`/`generation` is one the host authorized: a
    seat_request receipt for the row's id at the row's revision, of this job's incarnation and
    generation, whose args_sha is the canonical argv of the row now and whose decision fields
    still match the row (receipts.matches). False for anything missing, unreadable or changed."""
    row = board.seat(job, generation, seat)
    if row is None:
        return False
    led = ledger.load(board_key, job)
    if led is None or not led.incarnation:
        return False
    r = receipts.find(board_key, job, "seat_request", str(row.id), rev=row.revision,
                      incarnation=led.incarnation, generation=generation,
                      now=time.time() if now is None else now)
    if r is None or r.args_sha != receipts.args_sha(canonical_seat_args(row)):
        return False
    return receipts.matches(r, seat_decision(row))
