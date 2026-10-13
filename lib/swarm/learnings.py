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
  memory.c.<hash>    a learnings step covered that agent: its last contact as the step saw it (per agent)
  memory.declared_at the last `swarm remember --none` of an agent of the job
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
DECLARED_KEY = "memory.declared_at"
NO_LEARN_KEY = "memory.no_learn"
UNHARVESTED_PREFIX = "memory.u."
COVERED_PREFIX = "memory.c."
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
ID_WORDS = ("job", "agent", "session", "run", "host", "seat", "key", "id", "uid", "uuid", "user", "parent",
            "pid", "build", "pr", "issue", "ticket", "commit", "sha", "task", "launch", "attempt", "gen", "rev")
ID_LIKE = re.compile(r".*[:=/].*"                                   # <prefix>:<x> (and = or /)
                     r"|(?:" + "|".join(ID_WORDS) + r")(?:[-_.].*)?"  # <id word>, <id word>-<x>: run-7, session-xyz
                     r"|.*(?:^|[-_.])\d+(?:[-_.]|$).*"                # a numeric part: run-7, 2026, build-12-x
                     r"|.*\d{4}-?\d{2}-?\d{2}.*"                     # a date
                     r"|.*[0-9a-f]{8,}.*"                              # a hex or uuid run
                     r"|.*[0-9a-f]{4,}[-_][0-9a-f]{4,}.*")             # uuid-like groups


class ScopeUnreadable(Exception):
    """The job's subject tags could not be read: hindsight.remember refuses (a HindsightError, not stored)."""


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
    """Store the job's memory scope (activation). An empty list keeps whatever the job had. Raises when it
    could not be saved (the activation then fails: never a job running without the scope it asked for)."""
    if tags and not board.set_job_data(job, TAGS_KEY, ",".join(tags)[:1900]):
        raise ValueError(f"the subject tags of job {job} were not saved (no such job)")
    if topic and topic.strip() and not board.set_job_data(job, TOPIC_KEY, " ".join(topic.split())[:1900]):
        raise ValueError(f"the topic of job {job} was not saved (no such job)")


def job_tags(board, job: str, data: dict | None = None, js=None) -> list[str]:
    """The job's subject tags (the start recall's filter, what its memories are stamped with). Only when they
    were never set: its explicit project's name. A set emptied on purpose stays empty. Never the job's name."""
    data = board.job_data(job) if data is None else data
    tags = []
    for t in (data.get(TAGS_KEY) or "").split(","):
        try:
            tags.append(check_tag(t)) if t else None
        except ValueError:
            continue
    if TAGS_KEY in data:
        return list(dict.fromkeys(tags))    # set (an explicitly emptied set stays empty)
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
    except Exception as exc:     # fail closed: a memory is never stored without its job's subjects
        raise ScopeUnreadable(f"the subject tags of job {job} cannot be read now ({type(exc).__name__}); "
                              f"nothing was stored, retry") from exc
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


def note_declared(board, job: str) -> None:
    """An agent of the job declared it has no durable learnings (`swarm remember --none`)."""
    board.set_job_data(job, DECLARED_KEY, _now(board).isoformat())


def declared_this_run(board, js, data: dict | None = None) -> bool:
    """A declaration of this run counts as the job's learnings being accounted for (with every agent
    harvested or declared, which close_blockers checks separately)."""
    data = board.job_data(js.job) if data is None else data
    at, start = _ts(data.get(DECLARED_KEY)), _start(js)
    return at is not None and (start is None or at >= start)


# --- unharvested agents ------------------------------------------------------------------------

def _hkey(agent_key: str) -> str:
    return UNHARVESTED_PREFIX + hashlib.sha256(agent_key.encode()).hexdigest()[:16]


def _ckey(agent_key: str) -> str:
    return COVERED_PREFIX + hashlib.sha256(agent_key.encode()).hexdigest()[:16]


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


def _has_evidence(state) -> bool:
    """The agent stored a memory, or declared none, during its run (its row's remembered_at)."""
    return state is not None and state.remembered_at is not None and state.remembered_at >= state.joined_at


def mark_unharvested(board, job: str, agent_key: str, name: str, why: str, seen=None) -> bool:
    """Record that `agent_key` ended (or is ending) without storing or declaring its learnings. Idempotent;
    nothing when it stored something during its run. `seen`: its last contact, which a learnings step
    covering it records as its own coverage. Returns whether a marker exists now."""
    if _has_evidence(board.sync_state(agent_key)):
        return False
    key = _hkey(agent_key)
    data = board.job_data(job)
    if key in data:
        return True
    value = json.dumps({"key": agent_key, "name": name, "why": (why or "ended")[:200],
                        "at": _now(board).isoformat(),
                        "seen": seen.isoformat() if hasattr(seen, "isoformat") else seen})
    return bool(board.set_job_data(job, key, value[:1900]))


def clear_own_marker(board, job: str, name: str) -> None:
    """The active agent `name` of the job stored a memory or declared none: its own unharvested marker (a
    refused close may have set it while it was still working) is cleared. Never raises."""
    try:
        for a in board.agents(job, include_departed=False):
            if a.name == name:
                key = _hkey(a.agent_key)
                if key in board.job_data(job):
                    board.set_job_data(job, key, None)
    except Exception:
        pass


def sweep(board, job: str, *, active_too: bool = True) -> list[str]:
    """Mark every agent of this run of the job that has no evidence of its learnings: it stored nothing and
    declared nothing (remembered_at), and no learnings step ran after everything it did. Every ended agent
    is checked against that evidence, a normally completed one too (its stop gate may have failed). The
    agents still active count as well (`active_too`, the default: a close or a learnings step covers what
    they did so far; one that keeps working past it is checked again at the next close). Returns the names
    newly marked."""
    marked = []
    data = board.job_data(job)
    js = board.job_status(job)
    start = _start(js)
    rows = board.agents(job)
    resumed = {a.resume_of for a in rows if a.resume_of}   # a replacement carried its run on
    for a in rows:
        if not _counts(a) or a.agent_key in resumed:
            continue
        if start is not None and a.joined_at is not None and a.joined_at < start:
            continue                       # an earlier run of the job (closed before this one opened)
        mark = _hkey(a.agent_key)
        if mark in data:
            # reconciled against current evidence: a marker set while it was working (a refused close) goes
            # once it stores or declares none
            if _has_evidence(board.sync_state(a.agent_key)):
                board.set_job_data(job, mark, None)
            continue
        covered = _ts(data.get(_ckey(a.agent_key)))
        if covered is not None and a.last_contact_at is not None and covered >= a.last_contact_at:
            continue                       # a learnings step covered this agent, and it did nothing since
        ended = a.ended_at is not None
        if not ended and not active_too and a.status != "dead":
            continue
        why = ((a.left_reason or a.status) if ended
               else "dead" if a.status == "dead" else "still working at close")
        if mark_unharvested(board, job, a.agent_key, a.name, why, seen=a.last_contact_at):
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
    """The learnings step ran over the job: for each unharvested agent it covered (the markers read before
    the step), record that agent's coverage (memory.c.<hash> = its last contact as seen then) and clear its
    marker. Coverage is per agent: one that joined or kept working after it is not covered."""
    for e in entries:
        if e.get("key"):
            board.set_job_data(job, _ckey(e["key"]), str(e.get("seen") or e.get("at") or _now(board).isoformat()))
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
    if not (stored_this_run(board, js, data) or harvested_since(board, js.job, _start(js), data)
            or declared_this_run(board, js, data)):
        out.append(f"nothing has been stored in project memory for this run of {js.job}, and no learnings step "
                   f"or declaration says there is nothing: store the job's learnings (`swarm learn --job "
                   f"{js.job} --bank BANK -`), record that there are none (`swarm learn --job {js.job} --none "
                   f"\"<reason>\"`), or a person closes it with `--no-learn \"<reason>\"`")
    return out


def learn_pending(board, cfg: dict, job: str, since: _dt.datetime | None) -> bool:
    """For the driver: the learnings step of a concluding drive job has not run since `since` (the
    terminal verdict) or left agents unharvested. Sweeps the ended agents first."""
    if not applies(cfg):
        return False
    sweep(board, job, active_too=True)
    data = board.job_data(job)
    return bool(unharvested(board, job, data)) or not harvested_since(board, job, since, data)
