"""After a not_met verdict: get the orchestrator to spawn the next round.

Only the orchestrating (main) session can spawn agents, and once the judge has ruled not met and
nobody is left working, nothing else would ever wake it: the job would sit open and idle. The
judge's own SubagentStop hook can't help (its output goes to the judge, not the parent), so this
runs in the hooks that fire in the main session, the ones with no agent_id:

- PreToolUse / PostToolUse (event "turn" / "done"): additionalContext, at most once per verdict
  (and again after [supervise] orphan_minutes while the job still sits idle); checked at most every
  CHECK_SECONDS, since it reads the board;
- Stop (event "session-stop"): `decision: block` with the brief as the reason, when the
  orchestrator is about to end its turn with the job idle. Once per continuation: a Stop that is
  itself a continuation (stop_hook_active) is let through, so an orchestrator that decides to
  escalate to the user can.

The condition, from the board alone (Board.job_status): the job is open with a goal, not waiting
(`swarm wait`), the judge's latest verdict is not_met, and no agent is started, running or idle
(dead, left and completed ones don't count). A met verdict, any live agent, or non-judge activity after the verdict within the orphan
window gives nothing. The window also bounds repeat reminders.

State is one small file beside the job's marker (state_path), removed with it.
"""
from __future__ import annotations

import contextlib
import json
import os
import time
from pathlib import Path

from swarm.textsafe import term_safe

CHECK_SECONDS = 15.0     # the tool-call hooks look at the board at most this often per job
REMIND_SECONDS = 300.0   # fallback if [supervise] orphan_minutes is absent
FIELD_MAX = 3000         # characters of the judge's reason / instructions shown


def state_path(marker: Path) -> Path:
    """Beside a job's marker: session_id, checked epoch, verdict key, shown epoch
    (swarm.cli.respawn_state_path, which also removes it with the marker)."""
    from swarm.cli import respawn_state_path
    return respawn_state_path(marker)


def _clip(text, n: int = FIELD_MAX) -> str:
    s = term_safe(text, keep_newlines=True).strip()
    return s if len(s) <= n else s[:n - 1] + "…"


def idle_not_met(js) -> bool:
    """Whether this JobStatus is a not_met job with no agent left at work. A waiting job
    (`swarm wait`) is not: its next round needs something agents can't do."""
    return bool(js is not None and js.status == "active" and js.goal and js.verdict == "not_met"
                and not js.waiting_on and not (js.started or js.running or js.idle))


def verdict_key(js) -> str:
    return f"{js.verdict_by}|{js.verdict_at.isoformat() if js.verdict_at else ''}"


def brief(js, job: str, spawn_tags: bool = True, informational: bool = False, details: bool = False) -> str:
    """The text the orchestrator is shown. `spawn_tags`: name the tag lines the children's
    prompts need to join the job (Claude Code reads them; Codex doesn't need them)."""
    judge = _clip(js.verdict_by or "the judge", 200)
    reason = _clip(js.verdict_reason or "no reason recorded")
    nxt = _clip(js.verdict_next or "(the judge gave no instructions: read the board and the verdict "
                "reason to decide what is missing)")
    if details:   # the judge left a full report (swarm verdict --details): the workers should read it
        nxt += f" (the judge's full report: `swarm verdict show --job {job}`; tell the workers to read it)"
    if informational:
        return (f'[swarm] job "{job}": judge {judge} ruled not met: {reason}. '
                f'The supervisor review pipeline starts the fix worker with: {nxt}.')
    text = (f"[swarm] job \"{job}\": judge {judge} ruled not met: {reason}. Spawn agents now with "
            f"these instructions: {nxt}, plus a new judge for the same goal; spawn more agents if "
            f"the work needs it. Don't leave the job idle.")
    if spawn_tags:
        text += (f" Put the line `[swarm job: {job}]` in every prompt, and `[swarm role: judge]` in "
                 f"the new judge's. Repeat until the judge rules met; tell the user only if a round "
                 f"makes no progress.")
    else:
        text += (" Give the new judge a task name `judge__<task>` (for example, "
                 "`judge__round2`; the task suffix must be nonempty). Repeat until the judge rules "
                 "met; tell the user only if a round makes no progress.")
    return text


def _read_state(d, name: str) -> dict:
    from swarm import safefs
    try:
        data = json.loads(safefs.read_text(d, name, 4096) or "{}")
    except (ValueError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(d, name: str, state: dict) -> None:
    from swarm import safefs
    safefs.write_atomic(d, name, json.dumps(state), 0o600)


def check(marker: Path, job: str, *, force: bool, host, open_board,
          now: float | None = None, cfg: dict | None = None, session_id: str | None = None,
          informational: bool = False) -> str | None:
    """The brief for `job` if it is due, else None; a brief returned is recorded as shown.
    `force` (Stop): bypass the check throttle, never the already-shown test. `open_board`: a callable giving the
    board's context manager. Raises on board or file trouble: the caller logs it."""
    from swarm import safefs
    now = time.time() if now is None else now
    d = safefs.open_base(marker.parent, create=False)
    try:
        name = state_path(marker).name
        st = _read_state(d, name)
        if st.get("session_id") != session_id:
            st = {"session_id": session_id}
        # Keep the existing five-minute default when supervise has no orphan setting.
        interval = float(((cfg or {}).get("supervise") or {}).get("orphan_minutes", REMIND_SECONDS / 60)) * 60
        if not force and now - float(st.get("checked") or 0) < CHECK_SECONDS:
            return None
        activity, has_details = None, False
        with open_board() as board:
            js = board.job_status(job)
            has_details = idle_not_met(js) and board.verdict_details(job) is not None
            if idle_not_met(js) and js.verdict_at:
                agents = board.agents(job)
                judges = {a.name for a in agents if a.role == "judge"} | {js.verdict_by}
                # Display names are recycled after departure. Classify contacts by the
                # recorded agent role, so a worker reusing a judge's name still counts.
                times = [t for a in agents if a.role != "judge"
                         for t in (a.joined_at, a.last_contact_at, a.last_post_at) if t and t > js.verdict_at]
                times += [m.created_at for m in board.messages_after(0, job)
                          if m.agent_name not in judges and m.created_at > js.verdict_at]
                activity = max(times, default=None)
        st["checked"] = now
        text = None
        if idle_not_met(js):
            key = verdict_key(js)
            recent_activity = activity is not None and (interval <= 0 or now - activity.timestamp() < interval)
            already_shown = st.get("key") == key
            due_again = interval > 0 and now - float(st.get("shown") or 0) >= interval
            # Stop observes tool-hook suppression; a later worker contact starts a new idle window.
            if not recent_activity and (not already_shown or due_again):
                text = brief(js, job, host.reads_prompt_tags, informational, has_details)
                st["key"], st["shown"] = key, now
        with contextlib.suppress(OSError):   # if it can't be remembered, better said twice than never
            _write_state(d, name, st)
        return text
    finally:
        os.close(d)
