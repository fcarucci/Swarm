"""Pause a job and resume it, possibly on another box.

`swarm pause --job J`: Board.pause_job marks the job paused (nobody can join or post), records a
resume manifest of every active agent and closes their rows; then the final transcripts are
captured (this box's agents directly; other boxes' agents store theirs from their own hooks when
the pause stops them, which is waited for a little) and the pause is announced on the board.

`swarm resume --job J [--host H]`: for each manifest agent, hosts.resume.restore turns the
transcript stored ON THE BOARD (never a file on the old box) into a session of this box's host
(or a briefing when the harness differs or the copy is unusable); a restart row and a resume
marker bind that session to the agent's old name (the supervisor's own enrolment machinery,
hooks._enrol_resumed), the name is claimed before the job reopens so nobody can take it, the job
is reopened (Board.begin_resume) and the sessions are started detached. The outcome of each agent
is stored on the pause row; `swarm resume --retry` redoes the agents that failed.

Nothing here prints transcript text or secrets: transcripts on the board are already redacted,
and the report only has names, hosts, modes and short reasons.
"""
from __future__ import annotations

import dataclasses
import getpass
import os
import socket
import time

PAUSE_REASON_MAX = 300
WAIT_DEFAULT = 15.0          # seconds `swarm pause` waits for other boxes' final transcripts
ENROL_WAIT = 60.0            # seconds `swarm resume` waits to see each agent back on the board
PAUSE_RESUME_REASON = "pause-resume:"   # Restart.reason of a restart that is a resume (hooks know it)
RESUME_MINUTES = 60.0        # the cap recorded on a resume's restart row (no runner enforces it)


def current_user() -> str:
    try:
        return os.environ.get("USER") or getpass.getuser()
    except Exception:
        return "unknown"


def paused_stop_text(job: str, paused_by=None, reason=None) -> str:
    """What an agent of a paused job is told on its next tool call: stop, say nothing is lost."""
    why = f" ({reason})" if reason else ""
    by = f" by {paused_by}" if paused_by else ""
    return (f"[swarm] Job \"{job}\" was paused{by}{why}. Your work is saved: your transcript is stored on the "
            f"board and you will be resumed under your own name when the job is resumed (swarm resume "
            f"--job {job}). Stop now: end your turn without further tool calls, and do not report your "
            f"task as done: say the job is paused.")


# --------------------------------------------------------------------------- pause

@dataclasses.dataclass
class PauseReport:
    record: object                         # the PauseRecord
    already: bool = False                  # the job was paused before this call
    transcripts: dict = dataclasses.field(default_factory=dict)   # agent_key -> final|snapshot|failed|missing
    notes: list = dataclasses.field(default_factory=list)

    def lines(self) -> list[str]:
        rec = self.record
        out = [f"{rec.job} is paused" + (" (already)" if self.already else "")
               + f" since {rec.paused_at.strftime('%Y-%m-%d %H:%M UTC')} by {rec.paused_by or '?'}"
               + (f": {rec.reason}" if rec.reason else "")]
        for e in rec.manifest.get("agents", []):
            state = self.transcripts.get(e["agent_key"], "missing")
            out.append(f"  {e['agent_name']:<22} {str(e.get('role') or '-'):<16} {str(e.get('harness') or '-'):<7} "
                       f"{str(e.get('host') or '-'):<16} transcript: {state}")
        out += [f"  {n}" for n in self.notes]
        out.append(f"resume it with: swarm resume --job {rec.job} [--host claude|codex] [--workdir DIR]")
        return out


def _transcript_states(board, job: str, keys) -> dict:
    states = {}
    rows = {t.agent_key: t for t in board.transcripts(job=job)}
    for key in keys:
        t = rows.get(key)
        if t is None:
            states[key] = "missing"
        elif getattr(t, "failed", None) and not t.raw_bytes:
            states[key] = "failed"
        else:
            states[key] = "final" if t.final else "snapshot"
    return states


def local_cwds(board, cfg: dict, job: str) -> dict:
    """agent_key -> working directory, for the agents of `job` enrolled on THIS box (the local
    enrolment records: board rows can't say where an agent works)."""
    out = {}
    try:
        from swarm import transcripts
        for a in board.agents(job, include_departed=False):
            rec = transcripts.enrolled(a, cfg)
            if rec is not None and rec.cwd:
                out[a.agent_key] = rec.cwd
    except Exception:
        pass
    return out


def pause(board, cfg: dict, job: str, reason: str | None = None, by: str | None = None,
          wait: float = WAIT_DEFAULT, sleep=time.sleep, clock=time.monotonic) -> PauseReport | None:
    """Pause `job`. None if there is no such job or it is closed (completed, cancelled, failed)."""
    from swarm.board import PAUSE_WRITER
    from swarm.textsafe import term_safe
    reason = (term_safe(reason).strip()[:PAUSE_REASON_MAX] or None) if reason else None
    by = by or current_user()
    before = board.job_status(job)
    if before is None or before.status not in ("active", "paused"):
        return None
    already = before.status == "paused"
    rec = board.pause_job(job, by, reason, None if already else local_cwds(board, cfg, job))
    if rec is None:
        return None
    report = PauseReport(rec, already)
    if not already:
        who = ", ".join(e["agent_name"] for e in rec.manifest["agents"]) or "no agents"
        board.post(job, PAUSE_WRITER, f"job paused by {by}" + (f": {reason}" if reason else "")
                   + f". Recorded for resume: {who}")
    keys = [e["agent_key"] for e in rec.manifest["agents"]]
    try:   # this box's agents (and the orchestrator slice), final now that they are closed
        from swarm import transcripts
        if transcripts.enabled(cfg):
            transcripts.capture_job(board, cfg, job, True, deadline=time.monotonic() + 30,
                                    warn=lambda m: report.notes.append(m))
        else:
            report.notes.append("transcripts are disabled ([transcripts] enabled = false): a resume can only "
                                "brief the agents from the manifest")
    except Exception as exc:
        report.notes.append(f"transcript capture failed: {type(exc).__name__}: {exc}")
    deadline = clock() + max(0.0, wait)
    report.transcripts = _transcript_states(board, job, keys)
    while wait > 0 and any(s != "final" for s in report.transcripts.values()) and clock() < deadline:
        sleep(min(1.0, max(0.0, deadline - clock())))   # other boxes' hooks store theirs on the stop
        report.transcripts = _transcript_states(board, job, keys)
    missing = [e["agent_name"] for e in rec.manifest["agents"] if report.transcripts.get(e["agent_key"]) != "final"]
    if missing:
        report.notes.append("no final transcript yet for " + ", ".join(missing) + ": a resume uses the latest "
                            "snapshot, or a briefing from the manifest alone")
    return report


# --------------------------------------------------------------------------- resume

@dataclasses.dataclass
class AgentResult:
    agent_key: str
    name: str
    status: str                  # launched | planned | failed | skipped
    mode: str | None = None      # resume | briefing
    harness: str | None = None
    session_id: str | None = None
    note: str | None = None

    def as_dict(self) -> dict:
        return {k: v for k, v in dataclasses.asdict(self).items() if v is not None}


@dataclasses.dataclass
class ResumeReport:
    job: str
    pause_id: int
    host_label: str
    results: list = dataclasses.field(default_factory=list)
    hints: list = dataclasses.field(default_factory=list)
    dry_run: bool = False
    reopened: bool = False

    @property
    def failed(self) -> bool:
        return any(r.status == "failed" for r in self.results)

    def lines(self) -> list[str]:
        head = ("would resume" if self.dry_run else "resumed") + f" {self.job} on {self.host_label}"
        out = [head]
        for r in self.results:
            out.append(f"  {r.name:<22} {r.status:<9} {(r.mode or '-'):<9} {(r.harness or '-'):<7}"
                       + (f" {r.note}" if r.note else ""))
        out += [f"  {h}" for h in self.hints]
        return out


def find_pause(board, job: str, retry: bool):
    """(pause record, reopen) to act on: the open pause of a paused job; with `retry`, the latest
    resumed pause with failed agents of a job that is active again."""
    js = board.job_status(job)
    if js is None:
        return None, False
    if js.status == "paused":
        rec = board.open_pause(job)
        return (rec, True) if rec else (None, False)
    if retry and js.status == "active":
        for rec in reversed(board.pauses(job)):
            if rec.resumed_at is not None and any(v.get("status") == "failed" for v in (rec.outcome or {}).values()):
                return rec, False
    return None, False


def _flat(manifest: dict, e: dict) -> dict:
    return {**e, "job": manifest["job"], "paused_at": manifest.get("paused_at"), "reason": manifest.get("reason"),
            "goal": (manifest.get("job_state") or {}).get("goal"),
            "task": e.get("task") or (manifest.get("job_state") or {}).get("task"),
            "minutes": RESUME_MINUTES}


def _briefing(cfg: dict, flat: dict, target: str, workdir, label: str, why: str):
    """A Restored for an agent whose transcript can't be used: a fresh headless agent of the same
    harness, told who it is, its task and where the board stands."""
    from swarm.hosts import resume as hr
    from swarm.supervisor import launch
    cwd = hr._workdir(flat, workdir)
    prompt = (hr.resume_note(flat, label)
              + "\n\nYour earlier conversation could not be restored (" + hr._clean(why, 200) + "), so you start "
              "fresh: read the board first to see what you and the others did.")
    model = launch.replacement_model(cfg, target, flat.get("role"),
                                     flat.get("model") if isinstance(flat.get("model"), str) else None)
    spec = launch.spec_for(cfg, target, prompt=prompt, workdir=cwd, model=model, minutes=RESUME_MINUTES)
    return hr.Restored(target, "briefing", spec.argv, cwd, prompt, spec.session_id, None, False, (why,))


def resume(board, cfg: dict, job: str, *, host: str | None = None, workdir: str | None = None,
           dest_root=None, only=(), dry_run: bool = False, retry: bool = False, label: str | None = None,
           by: str | None = None, start=None, enrol_wait: float | None = None,
           sleep=time.sleep, clock=time.monotonic) -> ResumeReport | None:
    """Resume the open pause of `job` here (None: nothing to resume). See the module docstring.
    `start(restored, env)` launches a session (default hosts.resume.start); tests pass a fake."""
    from swarm.board import PAUSE_WRITER
    from swarm.hosts import resume as hr
    from swarm.supervisor import markers, runner
    rec, reopen = find_pause(board, job, retry)
    if rec is None:
        return None
    label = label or socket.gethostname()
    by = by or current_user()
    manifest = rec.manifest
    report = ResumeReport(job, rec.id, label, dry_run=dry_run)
    prior = dict(rec.outcome or {})
    wanted = {n.casefold() for n in only}
    entries = []
    for e in manifest.get("agents", []):
        if wanted and e["agent_name"].casefold() not in wanted:
            continue
        if retry and (prior.get(e["agent_key"]) or {}).get("status") != "failed":
            continue
        if e.get("kind") == "orchestrator":
            sid = e.get("session_id")
            report.hints.append(f"{e['agent_name']} is the orchestrator session, not started here: resume it yourself"
                                + (f" (claude --resume {sid})" if sid and (e.get("harness") or "claude") == "claude" else ""))
            report.results.append(AgentResult(e["agent_key"], e["agent_name"], "skipped", note="orchestrator"))
            continue
        entries.append(e)
    if wanted:
        unknown = wanted - {e["agent_name"].casefold() for e in manifest.get("agents", [])}
        for n in sorted(unknown):
            report.hints.append(f"no agent named {n} in the pause manifest")
    plans = []   # (entry, restored, restart row, marker path)
    outcome = dict(prior)
    # ---- phase A: everything that can fail, while the job is still paused
    for e in entries:
        flat = _flat(manifest, e)
        target = host or e.get("harness") or "claude"
        res = AgentResult(e["agent_key"], e["agent_name"], "failed", harness=target)
        report.results.append(res)
        try:
            try:
                restored = hr.restore(board, flat, target, dest_root, host_label=label, cfg=cfg,
                                      workdir=workdir, write=not dry_run)
            except hr.ResumeUnsupported as exc:
                restored = _briefing(cfg, flat, target, workdir, label, str(exc))
            res.mode, res.session_id = restored.mode, restored.session_id
            res.note = "; ".join(restored.notes)[:200] or None
            if dry_run:
                res.status = "planned"
                continue
            reason = f"{PAUSE_RESUME_REASON}{rec.id}"
            mine = next((x for x in board.restarts(job=job, agent_key=e["agent_key"])
                         if x.old_agent_key == e["agent_key"]), None)
            if mine is not None and mine.reason == reason and mine.ended_at is None and \
                    (prior.get(e["agent_key"]) or {}).get("status") == "failed":
                r = mine           # a retry: the row of the failed attempt is still open, reuse it
            else:
                r = board.record_restart(job, e["agent_key"], e["agent_key"], reason, target, RESUME_MINUTES)
            if r is None:
                res.status, res.note = "skipped", "already resumed (a restart row exists for it)"
                continue
            marker = markers.write_resume_marker(cfg, job, r.id, resume_of=e["agent_key"], name=e["agent_name"],
                                                 harness=target, session_id=restored.session_id)
            if restored.session_id:
                board.set_restart_agent(r.id, restored.session_id)
                if board.claim_resume(restored.session_id, e["agent_key"], job) is None:
                    res.note = "its name is no longer free"
                    continue
            plans.append((e, restored, r, marker, res))
        except Exception as exc:
            res.note = f"{type(exc).__name__}: {exc}"[:200]
    if dry_run:
        return report
    # ---- phase B: reopen (atomic: a second resume loses)
    if reopen:
        if not board.begin_resume(job, rec.id, by, label):
            for _e, _r, row, marker, res in plans:
                board.finish_restart(row.id, "cancelled")
                markers.remove_resume_marker(marker)
                res.status, res.note = "failed", "the job was resumed by someone else meanwhile"
            return report
        report.reopened = True
        board.post(job, PAUSE_WRITER, f"job resumed by {by} on host {label}: "
                   + (", ".join(e["agent_name"] for e, *_ in plans) or "no agents to restart"))
    # ---- phase C: start the sessions
    start = start or hr.start
    try:
        from swarm.supervisor.settings import settings
        pass_env = settings(cfg).get("pass_env") or ()
    except Exception:
        pass_env = ()
    launched = []
    for e, restored, row, marker, res in plans:
        try:
            token = None
            if restored.harness == "codex" and not restored.session_id:
                import secrets
                token = secrets.token_hex(16)
                if not markers.set_resume_token(marker, token):
                    raise RuntimeError("its resume marker is gone")
            start(restored, runner._child_env(token, None, pass_env))
            res.status = "launched"
            launched.append((e, row, res))
        except Exception as exc:   # the restart row stays open for `--retry`; the pre-claimed seat is freed
            if restored.session_id:
                board.close_agent(restored.session_id, "resume failed")
            res.status, res.note = "failed", f"{type(exc).__name__}: {exc}"[:200]
    # see them come back: a restart row is closed once its agent has enrolled (the row only gates the enrolment)
    deadline = clock() + max(0.0, ENROL_WAIT if enrol_wait is None else enrol_wait)
    pending = list(launched)
    while pending:
        rows = {a.agent_key: a for a in board.agents(job)}
        still = []
        for e, row, res in pending:
            a = next((x for x in rows.values() if x.resume_of == e["agent_key"] and x.ended_at is None
                      and x.tool_calls > 0), None)
            if a is not None:
                board.finish_restart(row.id, "completed")
                res.note = (res.note + "; " if res.note else "") + "back on the board"
            else:
                still.append((e, row, res))
        pending = still
        if not pending or clock() >= deadline:
            break
        sleep(1.0)
    for e, row, res in pending:
        res.note = (res.note + "; " if res.note else "") + "started, not on the board yet"
    for r in report.results:
        outcome[r.agent_key] = r.as_dict()
    board.record_resume_outcome(rec.id, outcome)
    return report
