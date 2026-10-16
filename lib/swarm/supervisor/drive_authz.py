"""Receipt binding for artifact, verdict and person control records. Launch queues need no receipts."""
from __future__ import annotations

import datetime
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


def canonical_until(until) -> str | None:
    """The `until` of an `ask` receipt's decision fields: the question's expiry as UTC ISO text (the board may
    hand back the same instant in the session's own time zone; a naive time is UTC). None for no expiry. The
    snapshot reader must compare with this form."""
    if until is None:
        return None
    if until.tzinfo is None:
        until = until.replace(tzinfo=datetime.timezone.utc)
    return until.astimezone(datetime.timezone.utc).isoformat()


def requirements_digest(pins: dict) -> str:
    """The digest of pinned requirements (path -> blob id) a `GOAL-READY <candidate> req=<digest>` carries:
    sha256 of `path@blob` lines in path order. The ledger's pinned_requirements is the one source of it."""
    return hashlib.sha256("".join(f"{p}@{b}\n" for p, b in sorted(pins.items())).encode()).hexdigest()


