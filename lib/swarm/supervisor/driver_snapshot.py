"""The driver's snapshot reader (spec 3.2, 4.0; plan Task 7b): one read of a drive job from the board, the
host-private ledger and the receipts, into the pure model of driver_model (`Snapshot`).

Only receipted control records count (spec 4.0). A record is used when receipts.find finds the receipt for its
kind, record id and revision, at this board, job, incarnation and generation, unexpired, and receipts.matches
confirms that the board row still carries the receipted decision fields. Anything else is displayed by the board
but ignored here.

Where each record lives (the carriers the board has today):
- done (hand-off): a DONE message; record id = message id, rev 1.
- review: a `REVIEW <artifact> VERIFIED|CHANGES` message; record id = message id, rev 1.
- post: a FINALIZE_BLOCKED message (the fix trigger); record id = message id, rev 1.
- verdict (artifact): the job-data record of the artifact, which must carry its own `id` and `rev`.
- verdict (goal or ruling) and goal_ready: a JSON object under job data `drive.goal_verdict` / `drive.goal_ready`
  (count-and-chunks, as verdict_data stores them), which must carry `id` and `rev`.
- ask (question): an open blocker with a seat and generation; record id = blocker id, rev 1.
- seat_request: a seat row in state `requested`; record id = seat row id, rev = seat revision.
- land: the ledger's integrated[group] entry, which names its land record (`land_id`, `land_rev`).

The recipe keys come from the job's owner record (`drive_cfg`), never from config.toml (plan [G3])."""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import re
from typing import Any

from swarm import enrolment, receipts, review
from swarm.supervisor import driver_model as dm

GOAL_READY_KEY = "drive.goal_ready"
GOAL_VERDICT_KEY = "drive.goal_verdict"
_REVIEW = re.compile(r"REVIEW (\S+) (VERIFIED|CHANGES)(?=\s|$)")
_FINALIZE_BLOCKED = "FINALIZE_BLOCKED "
# The recipe keys a job takes from its owner record (fixed at activation). Used when the record lacks them.
RECIPE_DEFAULTS = {"recipe": "coding", "review_role": "reviewer", "review_gates": ()}


def drive_cfg(board_key: str, job: str, cfg: dict) -> dict:
    """`cfg` with the recipe keys (`recipe`, `review_role`, `review_gates`) taken from the job's owner record.
    config.toml never sets them: a key the owner record does not carry takes its recipe default."""
    owner = enrolment.find_job_owner(board_key, job)
    recipe = {}
    for key, default in RECIPE_DEFAULTS.items():
        value = getattr(owner, key, None) if owner is not None else None
        recipe[key] = default if value is None else value
    return {**cfg, **recipe}


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
        return found if found is not None and receipts.matches(found, row) else None


def _seats(board: Any, job: str, gen: int, rc: _Receipts) -> dict[str, dm.SeatView]:
    out = {}
    names = [f.name for f in dataclasses.fields(dm.SeatView)]
    for st in board.seats(job, gen, include_closed=True):
        if st.generation != gen:
            continue
        if st.state == "requested":
            row = {"kind": "seat_request", "id": str(st.id), "rev": st.revision, "role": st.role,
                   "model": st.model, "brief": st.brief_path, "workdir": st.workdir}
            if rc.get("seat_request", st.id, st.revision, row) is None:
                continue                                  # an unreceipted request is displayed, not admitted
        out[st.seat] = dm.SeatView(**{n: getattr(st, n) for n in names})
    return out


def _verdict_view(rec: dict, rc: _Receipts) -> dm.VerdictView | None:
    rid, rev = rec.get("id"), rec.get("rev", 1)
    row = {"kind": "verdict", "id": str(rid), "rev": rev, "verdict": rec.get("verdict"),
           "scope": rec.get("scope"), "generation": rec.get("generation"),
           "candidate": rec.get("candidate"), "req": rec.get("req")}
    found = rc.get("verdict", rid, rev, row)
    if found is None:
        return None
    artifact = rec.get("artifact")
    return dm.VerdictView(
        verdict=rec.get("verdict"), scope=rec.get("scope") or "artifact", artifact=artifact,
        candidate=rec.get("candidate"), req=rec.get("req"), generation=rec.get("generation"),
        open=rec.get("open") if type(rec.get("open")) is int else None,
        progress=rec.get("progress") if type(rec.get("progress")) is bool else None,
        reason=rec.get("reason") or "", next=rec.get("next_steps") or "", by=found.seat or rec.get("judge"),
        at=_ts(rec.get("at")), receipt_id=rid)


def _verdicts(board: Any, job: str, rc: _Receipts) -> dict[str, dm.VerdictView]:
    out = {}
    for artifact, rec in review.artifact_verdicts(board, job).items():
        view = _verdict_view(rec, rc)
        if view is not None:
            out[artifact] = view
    return out


def _handoffs(board: Any, msgs: list, job: str, rc: _Receipts) -> list[dm.HandoffView]:
    out = []
    for m in msgs:
        if not m.message.startswith("DONE "):           # review.latest_handoffs drops addressed messages
            continue
        for h in review.latest_handoffs(board, job, messages=[m]):
            row = {"kind": "done", "id": str(m.id), "rev": 1, "artifact": h.artifact,
                   "sha": _sha(h.artifact), "group": _group(h.artifact)}
            found = rc.get("done", m.id, 1, row)
            if found is not None:
                out.append(dm.HandoffView(artifact=h.artifact, group=_group(h.artifact),
                                          by=found.seat or h.agent_name, at=h.created_at, receipt_id=m.id))
    return out


def _reviews(msgs: list, rc: _Receipts) -> dict[str, str]:
    latest: dict[str, str] = {}
    for m in msgs:
        match = _REVIEW.match(m.message)
        if match is None:
            continue
        artifact, outcome = match.group(1), match.group(2).lower()
        row = {"kind": "review", "id": str(m.id), "rev": 1, "group": _group(artifact),
               "artifact": artifact, "outcome": outcome}
        if rc.get("review", m.id, 1, row) is not None:
            latest[artifact] = outcome                    # messages are in id order: the newest wins
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
    """The sha of a group's landing, only from a land receipt whose every destination is verified at that sha."""
    row = {"kind": "land", "id": str(entry.get("land_id")), "rev": entry.get("land_rev", 1),
           "destinations": entry.get("destinations")}
    found = rc.get("land", entry.get("land_id"), entry.get("land_rev", 1), row)
    if found is None or not found.destinations:
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


def _goal_verdict(data: dict, gen: int, rc: _Receipts) -> dm.VerdictView | None:
    rec = _json_value(data, GOAL_VERDICT_KEY)
    if rec is None or rec.get("scope") not in ("goal", "ruling") or rec.get("generation") != gen:
        return None
    return _verdict_view(rec, rc)


def _goal_ready(data: dict, gen: int, rc: _Receipts) -> dict | None:
    gr = _json_value(data, GOAL_READY_KEY)
    if gr is None or gr.get("generation") != gen:
        return None
    row = {"kind": "goal_ready", "id": str(gr.get("id")), "rev": gr.get("rev", 1), "generation": gen,
           "candidate": gr.get("candidate")}
    if rc.get("goal_ready", gr.get("id"), gr.get("rev", 1), row) is None:
        return None
    return {"id": gr.get("id"), "req": gr.get("req"), "candidate": gr.get("candidate"), "at": gr.get("at"),
            "added_requirements": list(gr.get("added_requirements") or [])}


def _questions(board: Any, job: str, gen: int, rc: _Receipts) -> dict[str, list[dm.Question]]:
    out: dict[str, list[dm.Question]] = {}
    for b in board.blockers(job, include_closed=False):
        if b.seat is None or b.generation != gen:
            continue
        row = {"kind": "ask", "id": str(b.id), "rev": 1, "question": b.reason,
               "until": b.until.isoformat() if b.until else None, "default": b.default_value}
        if rc.get("ask", b.id, 1, row) is None:
            continue
        out.setdefault(b.seat, []).append(dm.Question(
            id=b.id, job=job, seat=b.seat, generation=gen, text=b.reason, default=b.default_value,
            until=b.until, receipt_id=b.id))
    return out


def _wakes(board: Any, job: str, seats: dict[str, dm.SeatView], msgs: list, rc: _Receipts) -> dict[str, list[int]]:
    """Per seat, the ids above its wake watermark of receipted posts to its agent and of events to it."""
    events = board.pending_events(job)
    out: dict[str, list[int]] = {}
    for name, st in seats.items():
        agent = board.active_agent_name(st.agent_key) if st.agent_key else None
        if agent is None:
            continue
        mark = st.wake_watermark or 0
        ids = [m.id for m in msgs if m.to_agent == agent and m.id > mark
               and rc.get("post", m.id, 1, {"kind": "post", "id": str(m.id), "rev": 1, "to": m.to_agent,
                                            "body": m.message}) is not None]
        ids += [e.id for e in events if e.to == agent.lower() and e.id > mark]
        if ids:
            out[name] = sorted(ids)
    return out


def read(board: Any, led: Any, job: str, *, cfg: dict, now: dt.datetime) -> dm.Snapshot:
    """One consistent read of `job` for next_actions (spec 3.2). `led` is the job's host-private ledger.
    `cfg` is accepted for the caller's symmetry: the recipe keys come from drive_cfg, never from it."""
    del cfg
    gen = led.generation
    rc = _Receipts(led, job, gen, now)
    msgs = board.messages_after(0, job)
    stored = board.job_status(job)
    data = board.job_data(job)
    seats = _seats(board, job, gen, rc)
    handoffs = _handoffs(board, msgs, job, rc)
    verdicts = _verdicts(board, job, rc)
    rounds: dict[str, int] = {}
    for artifact, vd in verdicts.items():
        if vd.generation == gen:
            rounds[_group(artifact)] = rounds.get(_group(artifact), 0) + 1
    return dm.Snapshot(
        job=job, generation=gen, goal=stored.goal if stored else None,
        status=stored.status if stored else "missing", halted=led.halted is not None,
        ledger=led, seats=seats,
        live={i.seat for i in led.intents if i.state in ("claiming", "claimed") and i.seat},
        handoffs=handoffs, reviews=_reviews(msgs, rc), verdicts=verdicts,
        goal_verdict=_goal_verdict(data, gen, rc), goal_ready=_goal_ready(data, gen, rc),
        questions=_questions(board, job, gen, rc), wakes=_wakes(board, job, seats, msgs, rc),
        integrated=_integrated(led, rc, _blocked(msgs, handoffs, rc)), rounds=rounds, now=now)
