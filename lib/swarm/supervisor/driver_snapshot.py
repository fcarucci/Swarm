"""The driver's snapshot reader (spec 3.2, 4.0; plan Task 7b): one read of a drive job from the board, the
host-private ledger and receipts, into the pure model of driver_model (`Snapshot`).

Only receipted control records count (spec 4.0). A record is used when receipts.find finds the receipt for its
kind, record id and revision, at this board, job, incarnation and generation, unexpired, and receipts.matches
confirms that the board row still carries the receipted decision fields. Anything else is displayed by the board
but ignored here.

Where each record lives (the carriers written by the board, hook, goal-ready and land code):
- done (hand-off): a DONE message; record id = message id, rev 1.
- review: a `REVIEW <artifact> VERIFIED|CHANGES <report>` message; record id = message id, rev 1.
- verdict (artifact, goal, ruling): a `VERDICT <verdict>: <reason>` message; record id = its message id, rev 1.
  The judgment (artifact, scope, open, progress, candidate, req, message_id) is job data: the artifact's
  `pipeline.verdict.<hash>` record, and the goal or ruling records at `drive.verdict.<generation>.<scope>.<digest>`. A record
  binds through its own `message_id`, and the receipt for that message must bind its verdict fields and its artifact
  (for goal and ruling verdicts, the candidate). Text is not compared.
- goal_ready: a `GOAL-READY <candidate> req=<digest>` message; record id = message id. `req` must equal the digest
  of the ledger's pinned requirements; `added_requirements` come from the ledger, never from the message.
- post: a FINALIZE_BLOCKED message (the fix trigger); record id = message id, rev 1.
- ask (question): an open blocker with a seat and generation; record id = blocker id, rev 1.
- seat_request: a seat row in state `requested`; record id = seat row id, rev = seat revision. Rows in any other
  state are admitted only when the ledger knows the seat (an intent for it, or an owner).
- land: the ledger's integrated[group] entry, which names its land record (`land_id`, `land_rev`).

The recipe keys come from the job's owner record (`drive_cfg`), never from config.toml; a missing or unknown
recipe fails closed (DriveConfigError)."""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import re
from typing import Any

from swarm import enrolment, receipts, review
from swarm.supervisor import driver_model as dm
from swarm.supervisor.drive_authz import canonical_until, requirements_digest

GOAL_READY = re.compile(r"GOAL-READY (\S+) req=(\S+)")
_REVIEW = re.compile(r"REVIEW (\S+) (VERIFIED|CHANGES)(?=\s|$)")
_FINALIZE_BLOCKED = "FINALIZE_BLOCKED "
RECIPES = ("coding", "artifact")
_OWNER_RECIPE_KEYS = ("recipe", "review_role", "review_gates")


class DriveConfigError(RuntimeError):
    """A drive job's owner record does not give its recipe keys: the job is not driven (fails closed)."""


def drive_cfg(board_key: str, job: str, cfg: dict) -> dict:
    """`cfg` with the recipe keys (`recipe`, `review_role`, `review_gates`) taken from the job's owner record, where
    Task 6a stores them at activation (`Record.owner`). config.toml never sets them. A missing owner record, a missing
    key or an unknown recipe raises DriveConfigError: the driver holds the job rather than guess a recipe."""
    owner = enrolment.find_job_owner(board_key, job)
    details = getattr(owner, "owner", None) if owner is not None else None
    if not isinstance(details, dict):
        raise DriveConfigError(f"job {job!r}: no owner record with recipe details on this host")
    missing = [k for k in _OWNER_RECIPE_KEYS if k not in details]
    if missing:
        raise DriveConfigError(f"job {job!r}: owner record lacks {missing}")
    if details["recipe"] not in RECIPES:
        raise DriveConfigError(f"job {job!r}: owner recipe {details['recipe']!r} is not one of {list(RECIPES)}")
    return {**cfg, "recipe": details["recipe"], "review_role": details["review_role"],
            "review_gates": details["review_gates"]}


def _group(artifact: str) -> str:
    return artifact.partition("@")[0]


def _sha(artifact: str) -> str:
    return artifact.partition("@")[2]


def _ts(text: Any) -> dt.datetime | None:
    if not isinstance(text, str):
        return None
    try:
        return dt.datetime.fromisoformat(text)
    except ValueError:
        return None


def _json_value(data: dict, key: str) -> dict | None:
    """A JSON object stored under `key` as a chunk count and chunks (the verdict_data convention)."""
    try:
        count = int(data[key])
        if not 0 < count <= 10000:
            return None
        value = json.loads("".join(data[f"{key}.{i}"] for i in range(count)))
    except (KeyError, ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


class _Receipts:
    """The receipt check of one read: this board, job, incarnation and generation, at `now`."""

    def __init__(self, led: Any, job: str, generation: int, now: dt.datetime) -> None:
        self.board_key = led.board_key
        self.job = job
        self.incarnation = led.incarnation
        self.generation = generation
        self.now = now.timestamp()

    def get(self, kind: str, record_id: Any, rev: Any, row: dict) -> receipts.Receipt | None:
        """The receipt for this record when the board row `row` matches it, else None."""
        if type(record_id) is not int or type(rev) is not int:
            return None
        found = receipts.find(self.board_key, self.job, kind, str(record_id), rev=rev,
                              incarnation=self.incarnation, generation=self.generation, now=self.now)
        if row is None:     # the caller binds the receipt's content itself (a land journal: _landed)
            return found
        return found if found is not None and receipts.matches(found, row) else None


def _seats(board: Any, job: str, gen: int, rc: _Receipts, led: Any) -> dict[str, dm.SeatView]:
    """A requested row needs its seat_request receipt. Any other row is admitted only when this host's ledger knows
    the seat (an intent for it, or an owner), so a row written straight into the board is not trusted."""
    out = {}
    names = [f.name for f in dataclasses.fields(dm.SeatView)]
    prefix = f"gen{gen}|"
    known = {i.seat for i in led.intents
             if i.seat and i.outcome not in ("lost", "halted") and (i.action_id or "").startswith(prefix)}
    known |= set((led.owners or {}).values())
    for st in board.seats(job, gen, include_closed=True):
        if st.generation != gen:
            continue
        if st.state == "requested":
            row = {"kind": "seat_request", "id": str(st.id), "rev": st.revision, "role": st.role,
                   "model": st.model, "brief": st.brief_path, "workdir": st.workdir}
            if rc.get("seat_request", st.id, st.revision, row) is None:
                continue                                  # an unreceipted request is displayed, not admitted
        elif st.seat not in known:
            continue                                      # a row the ledger never launched or owned
        out[st.seat] = dm.SeatView(**{n: getattr(st, n) for n in names})
    return out


def _handoffs(msgs: list, since: dt.datetime | None, rc: _Receipts) -> list[dm.HandoffView]:
    out = []
    for m in msgs:
        h = review.handoff_of(m, since)           # judged one message at a time: an unreceipted newer DONE
        if h is None:                             # never hides an older receipted one
            continue
        row = {"kind": "done", "id": str(m.id), "rev": 1, "artifact": h.artifact,
               "sha": _sha(h.artifact), "group": _group(h.artifact)}
        found = rc.get("done", m.id, 1, row)
        if found is not None:
            out.append(dm.HandoffView(artifact=h.artifact, group=_group(h.artifact),
                                      by=found.seat or h.agent_name, at=h.created_at, receipt_id=m.id))
    return out


def _reviews(msgs: list, rc: _Receipts) -> dict[str, str]:
    latest: dict[str, str] = {}
    for m in msgs:                                # id order: the newest receipted review wins
        match = _REVIEW.match(m.message)
        if match is None:
            continue
        artifact, outcome = match.group(1), match.group(2).lower()
        row = {"kind": "review", "id": str(m.id), "rev": 1, "group": _group(artifact),
               "artifact": artifact, "outcome": outcome}
        if rc.get("review", m.id, 1, row) is not None:
            latest[artifact] = outcome
    return latest


def _blocked(msgs: list, handoffs: list[dm.HandoffView], rc: _Receipts) -> dict[str, tuple[int, str]]:
    """Per group, the newest receipted FINALIZE_BLOCKED posted after the group's newest receipted hand-off."""
    newest_done: dict[str, int] = {}
    for h in handoffs:
        newest_done[h.group] = max(newest_done.get(h.group, 0), h.receipt_id or 0)
    out: dict[str, tuple[int, str]] = {}
    for m in msgs:
        if not m.message.startswith(_FINALIZE_BLOCKED):
            continue
        artifact = m.message[len(_FINALIZE_BLOCKED):].split(" ", 1)[0]
        group = _group(artifact)
        row = {"kind": "post", "id": str(m.id), "rev": 1, "to": m.to_agent, "body": m.message}
        if m.id > newest_done.get(group, 0) and rc.get("post", m.id, 1, row) is not None:
            out[group] = (m.id, m.message)
    return out


def _landed(entry: dict, rc: _Receipts) -> str | None:
    """The sha of a group's landing, only from a land receipt whose every destination is verified at that sha.
    The ledger entry names the receipt (`land_id`, as swarm.land writes it: the request's message id, a digit
    string; `land_rev`); the destinations are the receipt's own, bound by its content digest (as
    land.read_journal reads them), never the ledger's."""
    lid, rev = entry.get("land_id"), entry.get("land_rev", 1)
    if isinstance(lid, str) and re.fullmatch(r"[0-9]{1,18}", lid):
        lid = int(lid)
    if type(lid) is not int or type(rev) is not int:
        return None
    found = rc.get("land", lid, rev, None)
    if found is None or not isinstance(found.destinations, dict) or not found.destinations:
        return None
    if found.content_sha != receipts.content_sha("land", {"destinations": found.destinations}):
        return None
    sha = entry.get("landed")
    verified = all(isinstance(d, dict) and d.get("state") == "verified" and d.get("sha") == sha
                   for d in found.destinations.values())
    return sha if verified else None


def _integrated(led: Any, rc: _Receipts, blocked: dict[str, tuple[int, str]]) -> dict[str, dict]:
    out = {}
    for group in set(led.integrated or {}) | set(blocked):
        entry = dict((led.integrated or {}).get(group) or {})
        block = blocked.get(group)
        out[group] = {
            "landed": _landed(entry, rc) if entry.get("landed") else None,
            "blocked": block[1] if block else None,
            "blocked_id": block[0] if block else None,
            "withdrawn": entry.get("withdrawn") or None,
            "lease_held_since": entry.get("lease_held_since"),
        }
    return out


def _verdict_row(msg_id: int, rec: dict) -> dict:
    """The decision fields the verdict receipt binds: the verdict, its scope, generation, candidate, req, and the
    artifact (for a goal or ruling verdict, its candidate)."""
    return {"kind": "verdict", "id": str(msg_id), "rev": 1, "verdict": rec.get("verdict"),
            "scope": rec.get("scope"), "generation": rec.get("generation"), "candidate": rec.get("candidate"),
            "req": rec.get("req"), "artifact": rec.get("artifact") if rec.get("scope") == "artifact" else None}


def _bind_verdicts(records: list[dict], msgs: list, rc: _Receipts) -> list[dm.VerdictView]:
    """Bind each stored verdict record by its `message_id`. The message must exist, be unaddressed and start with
    VERDICT, and its receipt must match the record's decision fields, the artifact (goal and ruling: the candidate)
    included. Text is not compared: a receipt binds what was judged, not how the message reads. A message binds at
    most one record; a record without a message_id binds nothing."""
    by_id = {m.id: m for m in msgs}
    out = []
    for rec in records:
        mid = rec.get("message_id")
        if type(mid) is not int:
            continue
        m = by_id.get(mid)
        if m is None or m.to_agent or not m.message.startswith("VERDICT "):
            continue
        bound_to = rec.get("artifact") if (rec.get("scope") or "artifact") == "artifact" else rec.get("candidate")
        if not bound_to:
            continue                                  # an artifact verdict names its artifact; goal, its candidate
        found = rc.get("verdict", mid, 1, _verdict_row(mid, rec))
        if found is None:
            continue
        out.append(dm.VerdictView(
            verdict=rec.get("verdict"), scope=rec.get("scope") or "artifact",
            artifact=rec.get("artifact") if (rec.get("scope") or "artifact") == "artifact" else None,
            candidate=rec.get("candidate"), req=rec.get("req"), generation=rec.get("generation"),
            open=rec.get("open") if type(rec.get("open")) is int else None,
            progress=rec.get("progress") if type(rec.get("progress")) is bool else None,
            reason=rec.get("reason") or "", next=rec.get("next_steps") or "",
            by=found.seat or rec.get("judge"), at=_ts(rec.get("at")), receipt_id=mid))
    return out


def _drive_verdict_records(data: dict, gen: int, scope: str) -> list:
    """The goal or ruling verdict records of generation `gen`: the record at `drive.verdict.<gen>.<scope>`, and
    those review.verdict_data stores, one per (candidate, req), at `drive.verdict.<gen>.<scope>.<digest>`
    (chunked; review.drive_verdict_key). A stored record that does not parse or is not bound to its VERDICT
    message (review.bound_verdict) is skipped; every one is still bound by its receipt (_bind_verdicts)."""
    import re as _re
    want = _re.compile(rf"drive\.verdict\.{int(gen)}\.{_re.escape(scope)}\.[0-9a-f]{{32}}")
    out = []
    plain = _json_value(data, f"drive.verdict.{gen}.{scope}")    # the carrier contract's single-record key
    if isinstance(plain, dict):
        out.append(plain)
    for key in sorted(k for k in data if want.fullmatch(k)):
        try:
            rec = review.bound_verdict(review._chunked(data, key))
        except (KeyError, ValueError, TypeError):
            continue
        if isinstance(rec, dict) and key == review.drive_verdict_key(gen, scope, rec.get("candidate"), rec.get("req")):
            out.append(rec)
    return out


def _verdicts(board: Any, data: dict, job: str, gen: int, msgs: list,
              rc: _Receipts) -> tuple[dict[str, dm.VerdictView], dm.VerdictView | None]:
    """The receipted artifact verdicts, by artifact, and the newest receipted goal or ruling verdict of this
    generation (the records at drive.verdict.<gen>.<scope>.<digest of candidate and req>, each under its own key)."""
    artifacts = {v.artifact: v for v in _bind_verdicts(list(review.artifact_verdicts(board, job).values()), msgs, rc)}
    goal_records = []
    for scope in ("goal", "ruling"):
        for rec in _drive_verdict_records(data, gen, scope):
            if rec.get("scope") == scope and rec.get("generation") == gen:
                goal_records.append(rec)
    goals = _bind_verdicts(goal_records, msgs, rc)
    return artifacts, (max(goals, key=lambda v: v.receipt_id) if goals else None)


def _goal_ready(msgs: list, led: Any, gen: int, rc: _Receipts) -> dict | None:
    """The newest receipted GOAL-READY whose req is the digest of the ledger's pinned requirements. From the message
    only the candidate (receipt-bound) and the message id are taken."""
    pins = dict(led.pinned_requirements or {})
    if not pins:
        return None
    req = requirements_digest(pins)
    for m in reversed(msgs):
        found = GOAL_READY.fullmatch(m.message)
        if m.to_agent or found is None or found.group(2) != req:
            continue
        candidate = found.group(1)
        row = {"kind": "goal_ready", "id": str(m.id), "rev": 1, "generation": gen, "candidate": candidate}
        if rc.get("goal_ready", m.id, 1, row) is not None:
            return {"id": m.id, "req": req, "candidate": candidate, "at": m.created_at.isoformat(),
                    "added_requirements": [f"{p}@{b}" for p, b in sorted(pins.items())]}
    return None


def _questions(board: Any, job: str, gen: int, rc: _Receipts) -> dict[str, list[dm.Question]]:
    out: dict[str, list[dm.Question]] = {}
    for b in board.blockers(job, include_closed=False):
        if b.seat is None or b.generation != gen:
            continue
        row = {"kind": "ask", "id": str(b.id), "rev": 1, "question": b.reason,
               "until": canonical_until(b.until), "default": b.default_value}
        if rc.get("ask", b.id, 1, row) is None:
            continue
        out.setdefault(b.seat, []).append(dm.Question(
            id=b.id, job=job, seat=b.seat, generation=gen, text=b.reason, default=b.default_value,
            until=b.until, receipt_id=b.id))
    return out


def _wakes(board: Any, job: str, seats: dict[str, dm.SeatView], msgs: list, events: list, led: Any,
           rc: _Receipts) -> dict[str, list[int]]:
    """Per seat, the receipted posts and the events to its agent above that seat's own watermarks. Message ids and
    event ids are separate id spaces, so the ledger keeps a message watermark per seat (`msg:<seat>`) and an acknowledgement per event (`evack:<seat>:<id>`).
    The agent is looked up among departed members too: a resumable seat's agent has usually left."""
    names = {a.agent_key: a.name for a in board.agents(job, include_departed=True)}
    out: dict[str, list[int]] = {}
    for seat, st in seats.items():
        name = names.get(st.agent_key) if st.agent_key else None
        if name is None:
            continue                                    # nothing has joined for this seat yet
        msg_mark = led.watermarks.get(f"msg:{seat}", 0)
        ids = [m.id for m in msgs if m.to_agent == name and m.id > msg_mark
               and rc.get("post", m.id, 1, {"kind": "post", "id": str(m.id), "rev": 1, "to": m.to_agent,
                                            "body": m.message}) is not None]
        ids += [e.id for e in events if e.to and e.to.lower() == name.lower()
                and f"evack:{seat}:{e.id}" not in led.watermarks]   # acknowledged one by one, never a high-water mark
        if ids:
            out[seat] = sorted(ids)
    return out


def _rounds(verdicts: dict[str, dm.VerdictView], integrated: dict[str, dict], gen: int) -> dict[str, int]:
    """Judged rounds per artifact group still in flight: one per receipted artifact verdict of this generation (a
    re-judge of the same artifact@sha is the same record, so it is not a new round). A group whose newest verdict
    is met, or which is landed or withdrawn, is not in flight and gets no count."""
    counts: dict[str, int] = {}
    newest: dict[str, dm.VerdictView] = {}
    for artifact, vd in verdicts.items():
        if vd.generation != gen:
            continue
        group = _group(artifact)
        counts[group] = counts.get(group, 0) + 1
        if group not in newest or vd.receipt_id > newest[group].receipt_id:
            newest[group] = vd
    out = {}
    for group, judged in counts.items():
        entry = integrated.get(group) or {}
        if entry.get("landed") or entry.get("withdrawn") or newest[group].verdict == "met":
            continue
        out[group] = judged
    return out


def read(board: Any, led: Any, job: str, *, cfg: dict, now: dt.datetime) -> dm.Snapshot:
    """One consistent read of `job` for next_actions (spec 3.2). `led` is the job's host-private ledger. `cfg` is
    accepted for the caller's symmetry: the recipe keys come from drive_cfg, and the caller passes its result."""
    del cfg
    gen = led.generation
    rc = _Receipts(led, job, gen, now)
    data = board.job_data(job)
    since = _ts(data.get("pipeline.started_at"))
    msgs = board.messages_after(0, job)               # one scan per pass, one read of the events
    events = board.pending_events(job)
    stored = board.job_status(job)
    seats = _seats(board, job, gen, rc, led)
    handoffs = _handoffs(msgs, since, rc)
    verdicts, goal_verdict = _verdicts(board, data, job, gen, msgs, rc)
    integrated = _integrated(led, rc, _blocked(msgs, handoffs, rc))
    return dm.Snapshot(
        job=job, generation=gen, goal=stored.goal if stored else None,
        status=stored.status if stored else "missing", halted=led.halted is not None,
        ledger=led, seats=seats,
        live={i.seat for i in led.intents if i.state in ("claiming", "claimed") and i.seat},
        handoffs=handoffs, reviews=_reviews(msgs, rc), verdicts=verdicts,
        goal_verdict=goal_verdict, goal_ready=_goal_ready(msgs, led, gen, rc),
        questions=_questions(board, job, gen, rc),
        wakes=_wakes(board, job, seats, msgs, events, led, rc),
        integrated=integrated, rounds=_rounds(verdicts, integrated, gen), now=now)
