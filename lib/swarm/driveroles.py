"""Role names and prompt expectations for drive launches. No role grants filesystem restrictions.

Judges and reviewers keep the requested harness and model.
"""
from __future__ import annotations

import re

LEAD_ROLE = "engineering_lead"
JUDGE_ROLES = ("judge", "goal_judge", "ruling_judge")
REVIEW_ROLES = ("reviewer", "security", "security_reviewer")
READ_ONLY_ROLES = JUDGE_ROLES + REVIEW_ROLES
# Launches the driver names itself (driver_exec.launch_name): the name alone says what they are.
_JUDGE_LAUNCH = re.compile(r"(?:judge|goal-judge|ruling-judge)")
_REVIEW_LAUNCH = re.compile(r"reviewer-[a-z0-9_~-]*")
_LEAD_LAUNCH = re.compile(r"lead(?:~[0-9]+)?")
# The prompt kinds of an action id (`gen<N>|<kind>|<launch or ->|<group or ->|<round>|<trigger>`) that run a
# judge or a reviewer session.
JUDGE_KINDS = ("judge", "goal", "ruling")
REVIEW_KINDS = ("review",)



def is_read_only_role(role) -> bool:
    """Whether a launch role may only read: judges and reviewers (by exact role, or a `...judge` /
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


def launch_name_is_read_only(launch) -> bool:
    """A driver-named judge or reviewer launch (the names a launch cannot make: the driver creates them)."""
    return isinstance(launch, str) and bool(_JUDGE_LAUNCH.fullmatch(launch) or _REVIEW_LAUNCH.fullmatch(launch))


def launch_name_is_judge(launch) -> bool:
    return isinstance(launch, str) and _JUDGE_LAUNCH.fullmatch(launch) is not None


def launch_name_is_lead(launch) -> bool:
    """`lead` and its successors `lead~N`."""
    return isinstance(launch, str) and _LEAD_LAUNCH.fullmatch(launch) is not None


def action_kind(action_id) -> str | None:
    """The prompt kind slot of a driver action id (None for anything else, e.g. `requester:launch`)."""
    parts = action_id.split("|") if isinstance(action_id, str) else []
    return parts[1] if len(parts) > 1 and parts[0].startswith("gen") else None


def effective_harness(role, harness) -> tuple[str, str | None]:
    """Keep the requested harness for every role."""
    return harness, None
