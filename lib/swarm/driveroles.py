"""Which drive seats judge or review (read-only), which lead, and which harness they may run on (spec 3.12
[G1], 4.4). Pure and dependency-free: the board's seat requests, the hooks and the launch contract all
read the same rules, so none of them can drift.

Roles are the board's seat roles. A judge or reviewer never writes (Claude: edit tools disallowed and the
hook's write refusals; Codex: a read-only sandbox, which has no board access), so in drive mode those roles
always run on Claude, whatever a request asked for."""
from __future__ import annotations

import re

LEAD_ROLE = "engineering_lead"
JUDGE_ROLES = ("judge", "goal_judge", "ruling_judge")
REVIEW_ROLES = ("reviewer", "security", "security_reviewer")
READ_ONLY_ROLES = JUDGE_ROLES + REVIEW_ROLES
# Seats the driver names itself (driver_exec.seat_name): the name alone says what they are.
_JUDGE_SEAT = re.compile(r"(?:judge|goal-judge|ruling-judge)")
_REVIEW_SEAT = re.compile(r"reviewer-[a-z0-9_~-]*")
_LEAD_SEAT = re.compile(r"lead(?:~[0-9]+)?")
# The prompt kinds of an action id (`gen<N>|<kind>|<seat or ->|<group or ->|<round>|<trigger>`) that run a
# judge or a reviewer session.
JUDGE_KINDS = ("judge", "goal", "ruling")
REVIEW_KINDS = ("review",)

CODEX_OVERRIDE_REASON = ("a judge or reviewer seat runs on Claude in a drive job: Codex's read-only sandbox "
                         "has no board access, so the Codex request was overridden")


def is_read_only_role(role) -> bool:
    """Whether a seat role may only read: judges and reviewers (by exact role, or a `...judge` /
    `...reviewer` suffix such as `ruling_reviewer`)."""
    if not isinstance(role, str):
        return False
    r = role.lower()
    return r in READ_ONLY_ROLES or r.endswith(("judge", "reviewer"))


def is_judge_role(role) -> bool:
    """Whether the role may run `swarm verdict` (the job's judges)."""
    if not isinstance(role, str):
        return False
    r = role.lower()
    return r in JUDGE_ROLES or r.endswith("judge")


def seat_name_is_read_only(seat) -> bool:
    """A driver-named judge or reviewer seat (the names a seat request cannot make: the driver creates them)."""
    return isinstance(seat, str) and bool(_JUDGE_SEAT.fullmatch(seat) or _REVIEW_SEAT.fullmatch(seat))


def seat_name_is_judge(seat) -> bool:
    return isinstance(seat, str) and _JUDGE_SEAT.fullmatch(seat) is not None


def seat_name_is_lead(seat) -> bool:
    """`lead` and its successors `lead~N`."""
    return isinstance(seat, str) and _LEAD_SEAT.fullmatch(seat) is not None


def action_kind(action_id) -> str | None:
    """The prompt kind slot of a driver action id (None for anything else, e.g. `requester:seat`)."""
    parts = action_id.split("|") if isinstance(action_id, str) else []
    return parts[1] if len(parts) > 1 and parts[0].startswith("gen") else None


def effective_harness(role, harness) -> tuple[str, str | None]:
    """(harness, reason): a judge or reviewer requested on Codex runs on Claude, with the reason to post.
    Every other request keeps its harness and gives no reason."""
    if harness == "codex" and is_read_only_role(role):
        return "claude", CODEX_OVERRIDE_REASON
    return harness, None
