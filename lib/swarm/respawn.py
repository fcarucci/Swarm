"""After a not_met verdict: get the orchestrator to spawn the next round.

Only the orchestrating (main) session can spawn agents, and once the judge has ruled not met and
nobody is left working, nothing else would ever wake it: the job would sit open and idle. The
judge's own SubagentStop hook can't help (its output goes to the judge, not the parent), so this
runs in the hooks that fire in the main session, the ones with no agent_id:

- PreToolUse / PostToolUse (event "turn" / "done"): additionalContext, at most once per verdict
  (and again after REMIND_SECONDS while the job still sits idle); checked at most every
  CHECK_SECONDS, since it reads the board;
- Stop (event "session-stop"): `decision: block` with the brief as the reason, when the
  orchestrator is about to end its turn with the job idle. Once per continuation: a Stop that is
  itself a continuation (stop_hook_active) is let through, so an orchestrator that decides to
  escalate to the user can.

The condition, from the board alone (Board.job_status): the job is open with a goal, the judge's
latest verdict is not_met, and no agent is started, running or idle (dead, left and completed
ones don't count). A met verdict, or any agent still at work, gives nothing.

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
REMIND_SECONDS = 300.0   # ...and say it again after this long if the job is still idle
FIELD_MAX = 3000         # characters of the judge's reason / instructions shown


def state_path(marker: Path) -> Path:
    """Beside a job's marker: {"checked": epoch, "key": which verdict was shown, "shown": epoch}
    (swarm.cli.respawn_state_path, which also removes it with the marker)."""
    from swarm.cli import respawn_state_path
    return respawn_state_path(marker)


def _clip(text, n: int = FIELD_MAX) -> str:
    s = term_safe(text, keep_newlines=True).strip()
    return s if len(s) <= n else s[:n - 1] + "…"


def idle_not_met(js) -> bool:
    """Whether this JobStatus is a not_met job with no agent left at work."""
    return bool(js is not None and js.status == "active" and js.goal and js.verdict == "not_met"
                and not (js.started or js.running or js.idle))


def verdict_key(js) -> str:
    return f"{js.verdict_by}|{js.verdict_at.isoformat() if js.verdict_at else ''}"


def brief(js, job: str, spawn_tags: bool = True) -> str:
    """The text the orchestrator is shown. `spawn_tags`: name the tag lines the children's
    prompts need to join the job (Claude Code reads them; Codex doesn't need them)."""
    judge = _clip(js.verdict_by or "the judge", 200)
    reason = _clip(js.verdict_reason or "no reason recorded")
    nxt = _clip(js.verdict_next or "(the judge gave no instructions: read the board and the verdict "
                "reason to decide what is missing)")
    text = (f"[swarm] job \"{job}\": judge {judge} ruled not met: {reason}. Spawn agents now with "
            f"these instructions: {nxt}, plus a new judge for the same goal; spawn more agents if "
            f"the work needs it. Don't leave the job idle.")
    if spawn_tags:
        text += (f" Put the line `[swarm job: {job}]` in every prompt, and `[swarm role: judge]` in "
                 f"the new judge's. Repeat until the judge rules met; tell the user only if a round "
                 f"makes no progress.")
    else:
        text += (" Give the new judge a task name starting with `judge`. Repeat until the judge "
                 "rules met; tell the user only if a round makes no progress.")
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
          now: float | None = None) -> str | None:
    """The brief for `job` if it is due, else None; a brief returned is recorded as shown.
    `force` (Stop): no throttle and no already-shown test. `open_board`: a callable giving the
    board's context manager. Raises on board or file trouble: the caller logs it."""
    from swarm import safefs
    now = time.time() if now is None else now
    d = safefs.open_base(marker.parent, create=False)
    try:
        name = state_path(marker).name
        st = _read_state(d, name)
        if not force and now - float(st.get("checked") or 0) < CHECK_SECONDS:
            return None
        with open_board() as board:
            js = board.job_status(job)
        st["checked"] = now
        text = None
        if idle_not_met(js):
            key = verdict_key(js)
            if force or st.get("key") != key or now - float(st.get("shown") or 0) >= REMIND_SECONDS:
                text = brief(js, job, host.reads_prompt_tags)
                st["key"], st["shown"] = key, now
        with contextlib.suppress(OSError):   # if it can't be remembered, better said twice than never
            _write_state(d, name, st)
        return text
    finally:
        os.close(d)
