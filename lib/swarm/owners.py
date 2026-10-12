"""Who counts as the person behind a job: the names agents address a question for the owner to.

Configured by `[board] owner_names` (a list; empty by default), else git's `user.name` when that is set.
Recorded on the job at activation (job data `owner.names`, comma separated) so every backend, the
Postgres trigger included, reads the same list. The generic aliases `owner`, `user` and `human`, and the
job's creator, always count."""
from __future__ import annotations

import subprocess

JOB_DATA_KEY = "owner.names"


def owner_names(cfg: dict | None) -> list[str]:
    names = (((cfg or {}).get("board") or {}).get("owner_names")) or []
    if isinstance(names, str):
        names = [names]
    out = [str(n).strip() for n in names if str(n).strip() and "," not in str(n) and '"' not in str(n)]
    if out:
        return out
    try:
        r = subprocess.run(["git", "config", "--get", "user.name"], capture_output=True, text=True, timeout=2)
        name = r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        name = ""
    return [name] if name and "," not in name and '"' not in name else []


def stored(data: dict | None) -> list[str]:
    """The names recorded on a job (its job data)."""
    raw = (data or {}).get(JOB_DATA_KEY) or ""
    return [n.strip() for n in raw.split(",") if n.strip()]


ALIASES = ("user", "human", "owner")


def person_names(board, job: str, js=None) -> set[str]:
    """The casefolded names that address the person behind `job`: the aliases, the job's creator and the
    owner names recorded on the job. One rule for the stale sweeps (human_question) and for addressing."""
    if js is None:
        try:
            js = board.job_status(job)
        except Exception:
            js = None
    try:
        configured = stored(board.job_data(job))
    except Exception:
        configured = []
    creator = (getattr(js, "created_by", None) or "").casefold()
    return {*ALIASES, creator, *(n.casefold() for n in configured)} - {""}


def is_person(board, job: str, name: str | None) -> bool:
    """Whether `name` addresses the person behind `job` (person_names), ignoring case."""
    return bool(name) and name.casefold() in person_names(board, job)
