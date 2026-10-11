"""Person authority for drive control commands (spec 4.3 [G6], [F17], [F5]).

A control command (halt, unhalt, cancel, a person's seat request, a job-level wait, re-activation) is
allowed to exactly two callers:
  (a) the job's requester session: the session id recorded in the job's owner record (the session that
      ran `swarm activate`);
  (b) any other caller that is not an enrolled session of this board and confirms on /dev/tty.
An enrolled session (any agent a host user enrolled on this board) is refused outright, whatever it claims.
The tty path is a guardrail, not a boundary, against a same-user agent (spec 4.0)."""
from __future__ import annotations

import os

from swarm import enrolment, hosts


class PersonRefused(Exception):
    """A control command the caller may not run; the message says why and what to do instead."""


def _open_tty():
    return open("/dev/tty", "r+", encoding="utf-8", errors="replace")


def _tty_confirm(action: str, job: str) -> bool:
    """Ask on the controlling terminal; only a line reading `y` or `yes` confirms. No terminal: no."""
    try:
        tty = _open_tty()
    except OSError:
        return False
    try:
        tty.write(f"swarm: {action} job {job}? type y or yes to confirm: ")
        tty.flush()
        return tty.readline().strip().lower() in ("y", "yes")
    except OSError:
        return False
    finally:
        tty.close()


def session_of(env) -> str | None:
    """The calling host session's id (None when unknown: the caller then takes the tty path)."""
    return hosts.cli_session_id(env)


def require(action: str, job: str, *, board_key: str, env=None, confirm=None) -> str:
    """Return who may act: "requester" (the job's requester session) or "tty" (confirmed on /dev/tty).
    PersonRefused otherwise. `confirm(action, job) -> bool` replaces the tty prompt (tests)."""
    env = os.environ if env is None else env
    sid = session_of(env)
    if sid and sid in enrolment.agent_sessions(board_key):
        raise PersonRefused(f"refused: {action} is a person's action; this is an enrolled agent session, "
                            "and enrolled sessions never run it. Ask the job's lead (--to @EL) or a person.")
    owner = enrolment.find_job_owner(board_key, job)
    if sid and owner is not None and owner.session_id == sid:
        return "requester"
    if (confirm or _tty_confirm)(action, job):
        return "tty"
    raise PersonRefused(f"refused: {action} needs a person: run it from the session that activated {job}, "
                        "or from a terminal and confirm on the tty.")
