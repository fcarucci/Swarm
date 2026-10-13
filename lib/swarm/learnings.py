"""The memory life cycle of a job: its recall scope, what was stored, and the learnings step.

Everything here is deterministic bookkeeping on the board's per-job data (`Board.job_data`, which every
backend has); nothing calls an LLM, so the supervisor, the driver and the hooks may all use it.

Keys (job_data, `[a-z0-9_.-]`, at most 64 characters):
  memory.tags        the job's SUBJECT tags (comma separated; `swarm activate --tags`, `swarm tags`): what
                     the start recall filters on and what `swarm remember` / `swarm learn` stamp on the
                     job's memories (never the job name or any id: those are metadata)
  memory.topic       the job's topic, the start recall's query (else its task or description)
  memory.stored_at   the last time a memory of the job was stored (or queued): `hindsight.remember`
  memory.u.<hash>    an UNHARVESTED agent: it ended without the normal stop (killed, dead, lost, closed by
                     the supervisor) and without storing a memory or declaring it had none. JSON
                     {"key", "name", "why", "at"}. `swarm learn` removes it once it has run over the job.
  memory.harvest     the last learnings step: JSON {"at", "by", "agents", "facts", "none"}

The closing rules (`close_blockers`): a job may close only when no agent is unharvested and something was
stored for this run of it. The person-only `swarm deactivate --no-learn "<reason>"` waives both.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re

TAGS_KEY = "memory.tags"
TOPIC_KEY = "memory.topic"
STORED_KEY = "memory.stored_at"
HARVEST_KEY = "memory.harvest"
NO_LEARN_KEY = "memory.no_learn"
UNHARVESTED_PREFIX = "memory.u."
TAG = re.compile(r"[a-z0-9][a-z0-9._-]{0,39}")
MAX_TAGS = 8
LEARNER_ROLE = "learner"          # the drive seat that runs the learnings step; never itself unharvested
# Agent rows that are not workers of the job: the orchestrator and the driver's own one-write judge.
_NOT_AGENTS = ("orchestrator",)
_NOT_AGENT_PREFIXES = ("driver-", "learn:")
_NOT_ROLES = ("coordinator", LEARNER_ROLE)


def _now(board) -> _dt.datetime:
    try:
        return board.now()
    except Exception:
        return _dt.datetime.now(_dt.timezone.utc)


def _ts(text) -> _dt.datetime | None:
    if not text:
        return None
    try:
        t = _dt.datetime.fromisoformat(str(text))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=_dt.timezone.utc)


# --- scope: subject tags and topic -------------------------------------------------------------
#
# A job carries SUBJECT tags (`swarm activate --tags swarm,astro-loom`, editable with `swarm tags`): the systems
# and projects its memories are about. Its memories are retained with exactly those (plus any an agent adds with
# `--tags`). Never the job name, an agent name, a session, a run, a host or a date: unique tags defeat Hindsight's
# consolidation dedup (no two facts share a tag set), so duplicates pile up and recall slows (2026-09-08). Those
# are provenance and go into the memory's METADATA.

# What a tag must never look like: a namespaced id, a number, a date, a long hex or uuid run.
ID_LIKE = re.compile(r"(?:job|agent|session|run|host|seat|key|id|user|parent)[:=].*|.*\d{4}-\d{2}-\d{2}.*|"
                     r".*[0-9a-f]{8,}.*|\d+|.*:.*")


def check_tag(tag: str) -> str:
    t = str(tag or "").strip().lower()
    if not TAG.fullmatch(t):
        raise ValueError(f"invalid tag {tag!r}: 1-40 of a-z 0-9 . _ - starting with a letter or digit")
    if ID_LIKE.fullmatch(t):
        raise ValueError(f"tag {tag!r} looks like an id: a tag names a subject (a system or project), never a "
                         f"job, agent, session, run or date")
    return t


def parse_tags(text) -> list[str]:
    """`--tags a,b c` as subject tags (lower case); ValueError for one that is malformed or id-like."""
    return list(dict.fromkeys(check_tag(p) for p in re.split(r"[,\s]+", str(text or "").strip()) if p))[:MAX_TAGS]


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9._-]+", "-", str(text or "").lower()).strip("-.")[:40]


def _subject(text) -> str | None:
    """A project or repository name as a subject tag, or None when it would look like an id."""
    t = _slug(text)
    try:
        return check_tag(t) if t else None
    except ValueError:
        return None


def _repo_name(cwd: str) -> str | None:
    from swarm import gitinfo
    try:
        slug = gitinfo.repo_slug(cwd)
        if slug:
            return slug.rsplit("/", 1)[-1].removesuffix(".git")
        dirs = gitinfo.git_dirs(cwd)
        if dirs:
            common = dirs[1]
            top = os.path.dirname(common) if os.path.basename(common) == ".git" else common
            return os.path.basename(top.rstrip(os.sep))
    except Exception:
        return None
    return None


def derive_tags(project: str | None, cwd: str | None, explicit=()) -> list[str]:
    """The job's subject tags at activation: the explicit ones; else the explicit project's name; else the
    name of the git repository the job was activated in. Never the job's name."""
    tags = list(explicit)
    if not tags and project and _subject(project):
        tags.append(_subject(project))
    if not tags and cwd and _subject(_repo_name(cwd)):
        tags.append(_subject(_repo_name(cwd)))
    return list(dict.fromkeys(tags))[:MAX_TAGS]


def record_scope(board, job: str, tags: list[str], topic: str | None) -> None:
    """Store the job's memory scope (activation). An empty list keeps whatever the job had."""
    if tags:
        board.set_job_data(job, TAGS_KEY, ",".join(tags)[:1900])
    if topic and topic.strip():
        board.set_job_data(job, TOPIC_KEY, " ".join(topic.split())[:1900])


def job_tags(board, job: str, data: dict | None = None, js=None) -> list[str]:
    """The job's subject tags (the start recall's filter, what its memories are stamped with); with none
    recorded, its explicit project's name. Never the job's own name."""
    data = board.job_data(job) if data is None else data
    tags = []
    for t in (data.get(TAGS_KEY) or "").split(","):
        try:
            tags.append(check_tag(t)) if t else None
        except ValueError:
            continue
    if not tags:
        js = board.job_status(job) if js is None else js
        project = _subject(getattr(js, "project", None))
        tags = [project] if project else []
    return list(dict.fromkeys(tags))


def job_topic(board, job: str, js=None, data: dict | None = None) -> str:
    """The start recall's query: the job's topic, else its task, else its description, else its name."""
    data = board.job_data(job) if data is None else data
    from swarm import hindsight
    topic = data.get(TOPIC_KEY)
    return " ".join(topic.split())[:hindsight.QUERY_MAX] if topic else hindsight.recall_query(js, job)


def stamp_tags(board, job: str, extra=()) -> list[str]:
    """The tags a memory of the job is retained with: the job's subject tags plus `extra` (an agent's
    `--tags`), each checked once more. Never a job, agent, session, run or other id."""
    try:
        scope = job_tags(board, job)
    except Exception:
        scope = []
    out = []
    for t in [*scope, *extra]:
        try:
            out.append(check_tag(t))
        except ValueError:
            continue
    return list(dict.fromkeys(out))[:MAX_TAGS]


# --- what was stored ---------------------------------------------------------------------------

def note_stored(board, job: str) -> None:
    """A memory of the job was stored or queued (never fails the store)."""
    try:
        board.set_job_data(job, STORED_KEY, _now(board).isoformat())
    except Exception:
        pass


def _start(js) -> _dt.datetime | None:
    return None if js is None else (js.activated_at or js.created_at)


def stored_this_run(board, js, data: dict | None = None) -> bool:
    data = board.job_data(js.job) if data is None else data
    at, start = _ts(data.get(STORED_KEY)), _start(js)
    return at is not None and (start is None or at >= start)


# --- unharvested agents ------------------------------------------------------------------------

def _hkey(agent_key: str) -> str:
    return UNHARVESTED_PREFIX + hashlib.sha256(agent_key.encode()).hexdigest()[:16]


# left_reason of rows that were never an agent's own run: a placeholder a launch closes before its session
# takes the row over (claim_resume), or the driver's one-write judge.
_PLACEHOLDER_REASONS = ("drive predecessor", "pipeline predecessor", "pipeline judge seat busy",
                        "driver: verdict recorded")


def _counts(a) -> bool:
    """Whether an agent row is a worker whose learnings matter (not the orchestrator, a coordinator, a
    learner seat, the driver's own row or a launch placeholder)."""
    key = a.agent_key or ""
    return not (key in _NOT_AGENTS or key.startswith(_NOT_AGENT_PREFIXES) or (a.role or "") in _NOT_ROLES
                or key.startswith("pipeline-seat-") or (a.left_reason or "") in _PLACEHOLDER_REASONS)


def mark_ended(board, cfg: dict, job: str, a, why: str) -> None:
    """The supervisor or a runner closed agent `a` (stuck, dead, lost, a limit): with memory on, mark it
    unharvested unless it stored something. Never raises (a close is never failed by this)."""
    try:
        if applies(cfg) and _counts(a):
            mark_unharvested(board, job, a.agent_key, a.name, why)
    except Exception:
        pass


def mark_unharvested(board, job: str, agent_key: str, name: str, why: str) -> bool:
    """Record that `agent_key` ended abnormally without storing or declaring its learnings. Idempotent;
    nothing when it stored something during its run. Returns whether a marker exists now."""
    state = board.sync_state(agent_key)
    if state is not None and state.remembered_at is not None and state.remembered_at >= state.joined_at:
        return False
    key = _hkey(agent_key)
    data = board.job_data(job)
    if key in data:
        return True
    value = json.dumps({"key": agent_key, "name": name, "why": (why or "ended")[:200],
                        "at": _now(board).isoformat()})
    return bool(board.set_job_data(job, key, value[:1900]))


def sweep(board, job: str, *, active_too: bool = False) -> list[str]:
    """Mark every agent of the job that ended without the normal stop (any state but completed: killed,
    dead, lost, closed by the supervisor or a driver) and stored nothing. `active_too` (the job is being
    closed): the agents still active are ending now, without a stop of their own, and count as well. A
    derived-dead agent (silent past dead_minutes) counts always. Returns the names newly marked."""
    marked = []
    data = board.job_data(job)
    rows = board.agents(job)
    resumed = {a.resume_of for a in rows if a.resume_of}   # a replacement carried its run on
    try:
        harvested = _ts(json.loads(data.get(HARVEST_KEY) or "{}").get("at"))
    except (ValueError, AttributeError):
        harvested = None
    for a in rows:
        if not _counts(a) or _hkey(a.agent_key) in data or a.agent_key in resumed:
            continue
        if harvested is not None and a.last_contact_at is not None and harvested >= a.last_contact_at:
            continue                       # a learnings step ran after everything it did

        ended = a.ended_at is not None
        if ended and a.status == "completed":
            continue                       # the normal stop: its own gate ran (SubagentStop)
        if not ended and not active_too and a.status != "dead":
            continue
        why = (a.left_reason or a.status) if ended else ("dead" if a.status == "dead" else "job closing")
        if mark_unharvested(board, job, a.agent_key, a.name, why):
            marked.append(a.name)
    return marked


def unharvested(board, job: str, data: dict | None = None) -> list[dict]:
    data = board.job_data(job) if data is None else data
    out = []
    for k, v in sorted(data.items()):
        if k.startswith(UNHARVESTED_PREFIX):
            try:
                entry = json.loads(v)
            except ValueError:
                entry = {"name": "?", "why": "unreadable marker"}
            out.append({**entry, "_k": k})
    return out


def harvest(board, job: str, by: str, facts: int, none: str | None, entries: list[dict]) -> None:
    """The learnings step ran over the job: record it and clear the unharvested markers it covered."""
    for e in entries:
        board.set_job_data(job, e["_k"], None)
    record = {"at": _now(board).isoformat(), "by": by, "facts": facts,
              "agents": [e.get("name") for e in entries][:20], "none": (none or "")[:300] or None}
    board.set_job_data(job, HARVEST_KEY, json.dumps(record)[:1900])


def harvested_since(board, job: str, since: _dt.datetime | None, data: dict | None = None) -> bool:
    data = board.job_data(job) if data is None else data
    try:
        at = _ts(json.loads(data.get(HARVEST_KEY) or "{}").get("at"))
    except (ValueError, AttributeError):
        at = None
    return at is not None and (since is None or at >= since)


# --- closing -----------------------------------------------------------------------------------

def applies(cfg: dict) -> bool:
    """The life cycle applies with project memory on (and never in the directory edition)."""
    from swarm import edition, hindsight
    return hindsight.enabled(cfg) and not edition.is_directory()


def close_blockers(board, cfg: dict, js, *, active_too: bool = True) -> list[str]:
    """Why the job may not close yet: unharvested agents, nothing stored for this run. [] when it may
    (or the life cycle does not apply). Sweeps first, so an agent that ended abnormally is marked
    durably before the answer is given."""
    if js is None or not applies(cfg):
        return []
    sweep(board, js.job, active_too=active_too)
    data = board.job_data(js.job)
    out = []
    pending = unharvested(board, js.job, data)
    if pending:
        names = ", ".join(str(e.get("name")) for e in pending[:10]) + (" ..." if len(pending) > 10 else "")
        out.append(f"{len(pending)} agent(s) ended without storing their learnings and are unharvested "
                   f"({names}): run `swarm learn --job {js.job} --harvest`, then retain what they learned "
                   f"with `swarm learn --job {js.job} --bank BANK -` (or `--none \"<reason>\"`)")
    if not stored_this_run(board, js, data):
        out.append(f"nothing has been stored in project memory for this run of {js.job}: store the job's "
                   f"learnings (`swarm learn --job {js.job} --bank BANK -`), or a person closes it with "
                   f"`--no-learn \"<reason>\"`")
    return out


def learn_pending(board, cfg: dict, job: str, since: _dt.datetime | None) -> bool:
    """For the driver: the learnings step of a concluding drive job has not run since `since` (the
    terminal verdict) or left agents unharvested. Sweeps the ended agents first."""
    if not applies(cfg):
        return False
    sweep(board, job)
    data = board.job_data(job)
    return bool(unharvested(board, job, data)) or not harvested_since(board, job, since, data)
