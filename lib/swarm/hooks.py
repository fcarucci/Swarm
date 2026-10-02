"""Claude Code hook handlers for the swarm board.

  SubagentStart -> route the agent to one of the session's jobs (or defer that to its first tool
                   call, when its prompt becomes readable), allocate a unique name for this
                   agent_id and inject the board instructions,
                   the roster of the other agents, the job's recent messages and (with Hindsight
                   configured) the project's memories
  PreToolUse    -> inject what is new for this agent: unread messages (paged, with a count of
                   what is left), roster changes (a short diff, a full roster every
                   roster_refresh_minutes) and, with Hindsight, new memories every recall_minutes
                   and a reminder to store findings; nothing if nothing is new. Routes and
                   joins agents whose job was not known at SubagentStart, and agents already
                   running at activation only with --adopt-running; records the agent as
                   running with this tool in flight
                   A member's Agent call (spawning a subagent of its own) is refused unless it
                   is justified and within the [spawn] caps (see "spawning" below)
  PostToolUse   -> clear the in-flight tool (so a long command shows as running, not idle); a
                   shell call that saved a memory (swarm remember, or a writer under [provenance] writers) is
                   pinned to the agent's transcript (swarm.provenance)
  SubagentStop  -> mark the agent completed and release its name

A hook only acts while a swarm job is active: a marker file <marker_dir>/<job>.json written by
`swarm activate`, bound to one Claude session. With no marker the shell wrapper exits before
Python even starts, and a session with no marker of its own returns before any board is opened
(or its backend imported). A session may run several jobs: each subagent joins the one named by
the `[swarm job: <job>]` line in its spawn prompt (see "routing" below). A job with a goal has
one judge, the subagent whose prompt also carries `[swarm role: judge]`: it gets the judge's
instructions instead of the worker's, and the workers are told who it is.
All storage goes through the `board` package (one board opened per hook invocation). The
Hindsight client is imported only when `[hindsight] url` is set and a call is actually due.
Hooks must never break the agent: every failure is swallowed and the hook exits 0.
"""
from __future__ import annotations

import dataclasses
import re
import json
import os
import sys
import time
from pathlib import Path

from swarm.cli import JOB_TAG as TAG, ROLE_TAG, SPAWN_TAG, fmt, tag_line  # type: ignore
from swarm import hosts  # noqa: E402  (stdlib-only: hosts imports no board)
from swarm import compat


def _bin() -> str:
    """The swarm command agents are told to run: this plugin's own bin/swarm (shell-quoted)."""
    import shlex
    from swarm.paths import agent_bin
    return shlex.quote(str(agent_bin()))


ACTIVE_STATUSES = ("started", "running")
# Claude's; the hooks use current_host()
SPAWN_TOOLS = ("Agent", "Task")   # the tool a subagent spawns subagents with (Task: older name)
# Refused for verifiers: they check other agents' work and must not change it. Shell commands
# that look like they write are refused too (best effort, swarm.shellguard).
VERIFIER_DENIED = ("Edit", "Write", "MultiEdit", "NotebookEdit") + SPAWN_TOOLS
# Transcript work ([transcripts]) in one SubagentStart/Stop hook stops after this many seconds
# (the hooks' timeouts are 15 and 10); the per-tool hooks never do any.
TRANSCRIPT_BUDGET_SECONDS = 4.0
# Memory provenance in one PostToolUse (swarm.provenance), all of it: detection, the transcript
# tail, redaction, compression, the board write and an optional metadata patch.
PROVENANCE_BUDGET_SECONDS = 2.5


MARKER_MAX_BYTES = 64 * 1024   # a marker is a few hundred bytes; anything bigger is not one
_SKIPPED_LOGGED: set = set()   # marker names already logged as refused by this process


JOB_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")   # always fullmatch


def _valid_job(job) -> bool:
    """A job name a marker may name: JOB_NAME, as `activate` requires (markers are
    sandbox-writable: the name ends up in the commands shown to agents)."""
    return isinstance(job, str) and JOB_NAME.fullmatch(job) is not None


def _q(s) -> str:
    """A word as the shell must see it in a command shown to an agent: shlex.quote, always in
    quotes (so a plain name reads the same as before: 'J', 'Homer Simpson')."""
    import shlex
    q = shlex.quote(str(s))
    return q if q.startswith("'") else f"'{q}'"


def _markers(cfg: dict) -> list[dict]:
    """The job markers in the marker dir, read without following links or blocking (the dir is
    sandbox-writable: safefs.scan skips symlinks, hard links, FIFOs, other users' files and
    anything over MARKER_MAX_BYTES). A marker without a valid job name is ignored too."""
    from swarm import safefs
    d = Path(cfg["hook"]["marker_dir"]).expanduser()
    try:
        fd = safefs.open_base(d, create=False)
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        _log_line("markers", "-", f"marker dir {d} refused: {exc}")
        return []
    out, skipped = [], []
    try:
        entries = safefs.scan(fd, ".json", limit=MARKER_MAX_BYTES, skipped=skipped)
        mtimes = {name: safefs.mtime(fd, name) for name, _ in entries}
    except OSError as exc:
        _log_line("markers", "-", f"marker dir {d} unreadable: {exc}")
        return []
    finally:
        os.close(fd)
    for name in skipped:
        if name not in _SKIPPED_LOGGED:
            _SKIPPED_LOGGED.add(name)
            _log_line("markers", "-", f"skipped {name} in {d}: not a private regular file of this "
                                       f"user, or over {MARKER_MAX_BYTES} bytes")
    for name, data in entries:
        try:
            m = json.loads(data)
        except ValueError:
            continue
        if not isinstance(m, dict) or not _valid_job(m.get("job")):
            continue
        m["_path"], m["_mtime"] = d / name, mtimes.get(name)
        out.append(m)
    return out


MARKER_LOCK_SECONDS = 1.0   # how long a claim waits for another process's lock on a marker


def _replace_marker(path: Path, text: str, mode: int) -> None:
    """Replace the marker with `text` atomically (a fresh temp file in the same directory, then
    a rename relative to the verified directory: safefs.write_atomic), keeping its mode: readers
    never see it half-written, and nothing is written through a link."""
    from swarm import safefs
    with safefs.dir_fd(path.parent, create=False) as d:
        safefs.write_atomic(d, path.name, text, mode & 0o777)


def _claim(path: Path, session_id: str) -> str | None:
    """Atomically bind an unbound marker to session_id. Returns the job if this session owns it.
    Never waits more than MARKER_LOCK_SECONDS for another process's lock (then: None). Under
    the marker's lock (swarm.cli.locked_marker, which removals take too), so a marker removed
    meanwhile is not brought back."""
    from swarm.cli import locked_marker
    with locked_marker(path, MARKER_LOCK_SECONDS) as fh:
        if fh is None:
            return None
        m = json.loads(fh.read() or "{}")
        owner = m.get("session_id")
        if owner and owner != session_id:
            return None
        if not owner:
            m["session_id"] = session_id
            _replace_marker(path, json.dumps(m), os.fstat(fh.fileno()).st_mode & 0o7777)
        return m.get("job")


def _try_claim(path: Path, session_id: str) -> str | None:
    try:
        return _claim(path, session_id)
    except FileNotFoundError:  # deactivated meanwhile
        return None


def _session_markers(cfg: dict, session_id: str) -> tuple[dict, dict]:
    """({job: marker} bound to this Claude session, {job: marker} not bound to any session).
    Resume markers (a supervisor replacement's, with a "resume" section) are never in the
    unbound pool: one not bound yet waits for its runner to bind it (markers.bind_session), and
    no session may claim it by routing. A bound one counts for its own session only, and never
    in place of an ordinary marker of the same job."""
    bound, unbound = {}, {}
    for m in _markers(cfg):
        owner = m.get("session_id")
        resume = "resume" in m
        if owner == session_id:
            if not resume or m["job"] not in bound:
                bound[m["job"]] = m
        elif not owner and not resume:
            unbound[m["job"]] = m
    return bound, unbound


# --------------------------------------------------------------------------- routing
#
# One Claude session can run several swarm jobs. A subagent joins the job named by a line
# `[swarm job: <job>]` in its spawn prompt; with no tag it joins the session's only job, and
# with several jobs and no tag it joins none. Claude Code does not put the prompt in the
# SubagentStart payload and writes the subagent's transcript only after SubagentStart, so the
# tag is read from the transcript's first user message at the subagent's first tool call. The
# decision is cached on the board (Board.record_route) and, once it joins, in its agent row.

def _tag_of(prompt: str, prefix: str = TAG) -> str | None:
    """The value of the first `[swarm job: <job>]` line of a prompt (or of another tag, e.g.
    `[swarm role: <role>]` with prefix ROLE_TAG), if any."""
    for line in prompt.splitlines():
        s = line.strip()
        if s.startswith(prefix) and s.endswith("]") and s[len(prefix):-1].strip():
            return s[len(prefix):-1].strip()
    return None


_CURRENT: dict = {"host": None}


def current_host():
    """The Host of this hook invocation (set by _handle; Claude when called directly)."""
    return _CURRENT["host"] or hosts.get("claude")


def _spawn_prompt(payload: dict, agent_id: str) -> str | None:
    """The subagent's spawn prompt, or None while it cannot be read ("" if readable but without
    a user message): the host knows where (Claude: the subagent's own transcript, which does not
    exist yet at SubagentStart and does from the first PreToolUse on)."""
    return current_host().spawn_prompt(payload, agent_id)


def _role(payload: dict, agent_id: str, prompt: str | None) -> str | None:
    """The role the spawn asked for: its [swarm role: ...] tag, or where the host can't show the
    prompt's tags (Codex), the host's hint (the task name)."""
    from swarm import roles
    host = current_host()
    return (roles.from_prompt(prompt or "") if host.reads_prompt_tags else None) or host.role_hint(payload, agent_id)


def _pick(tag: str | None, bound: dict, unbound: dict, session_id: str) -> tuple[str | None, str]:
    """(job, why not): the tagged job if it is bound to this session or claimable (in
    `unbound`, which the caller has cut down to what this agent may claim); with no tag the
    only bound job, or with none bound the only claimable one. Claims the marker it picks."""
    if tag is not None:
        if tag in bound or (tag in unbound and _try_claim(unbound[tag]["_path"], session_id) == tag):
            return tag, ""
        return None, f'tagged {tag_line(tag)}, but "{tag}" is not an active swarm job of this Claude session'
    pool = bound or unbound
    if len(pool) == 1:
        job = next(iter(pool))
        if job in bound or _try_claim(pool[job]["_path"], session_id) == job:
            return job, ""
        return None, f'"{job}" was bound to another session meanwhile'
    if not pool:
        return None, "no swarm job is active in this session"
    return None, (f"{len(pool)} swarm jobs are active in this session ({', '.join(sorted(pool))}) "
                  f"and the prompt has no {TAG} <job>] tag")


def _log_route(agent_id: str, text: str) -> None:
    """Why an agent joined no board: once per agent (the decision is cached). Never raises."""
    _append_host_log("routing.log", f"{time.strftime('%F %T')} {agent_id}: {text}")


def _refuse(event: str, agent_id: str, why: str, tell_agent: bool) -> None:
    _log_route(agent_id, f"not joining: {why}")
    if tell_agent:  # it was meant for a swarm: say so, so the orchestrator hears about it
        _out("SubagentStart" if event == "start" else "PreToolUse",
             f"[swarm] Your prompt is {why}, so you are on no swarm board. Say so in your report.")


def _resumed_job(route, bound: dict) -> str | None:
    """The job of a departed member being resumed, unless routing since sent it elsewhere."""
    if route.member_job in bound and not (route.state == "final" and route.job != route.member_job):
        return route.member_job
    return None


def _route_unreadable_start(board, agent_id: str, sid: str, bound: dict, unbound: dict,
                            payload: dict, cfg: dict) -> None:
    """SubagentStart of a fresh spawn: its prompt is not readable yet. With one job in sight,
    join it now (checked against the tag at the first tool call); with several, wait for the
    first tool call when the tag can be read."""
    every = {**unbound, **bound}
    if len(every) == 1:
        job, m = next(iter(every.items()))
        if job in bound or _try_claim(m["_path"], sid) == job:
            if m.get("goal"):  # the judge tag is unreadable too: enrol it once it can be read
                board.record_route(agent_id, sid, "pending")
                return
            board.record_route(agent_id, sid, "unverified", job)
            _enrol(board, "start", agent_id, job, payload, cfg, sid, m)
        return
    if bound:
        board.record_route(agent_id, sid, "pending")
        return
    # Several unbound markers: a tool call may not claim, and at SubagentStart the tag is unknown.
    board.record_route(agent_id, sid, "final", None)
    _refuse("start", agent_id, f"{len(every)} swarm jobs ({', '.join(sorted(every))}) are bound to no "
            f"session and the tag is not readable at SubagentStart; activate them from inside Claude "
            f"Code (or with --session)", False)


def _route_new(board, event: str, agent_id: str, sid: str, bound: dict, unbound: dict,
               payload: dict, cfg: dict) -> None:
    """Route an agent with no active board row and enrol it (or say why not, once)."""
    route = board.route(agent_id)
    resumed = _resumed_job(route, bound)
    started_here = event == "start" or (route.state in ("pending", "unverified") and route.session_id == sid)
    if event == "turn" and not started_here:
        if resumed:
            _enrol(board, event, agent_id, resumed, payload, cfg, sid, bound[resumed])
            return
        if route.state == "final":  # decided before: nothing to do
            return
        # Running since before the activation: only jobs activated with --adopt-running take it.
        bound = {j: m for j, m in bound.items() if m.get("adopt_running")}
        unbound = {j: m for j, m in unbound.items() if m.get("adopt_running")}
        if not bound and not unbound:
            return
    prompt = _spawn_prompt(payload, agent_id)
    if prompt is None and event == "start":
        if resumed:
            _enrol(board, event, agent_id, resumed, payload, cfg, sid, bound[resumed])
        else:
            _route_unreadable_start(board, agent_id, sid, bound, unbound, payload, cfg)
        return
    tag = _tag_of(prompt or "")
    job, why = (resumed, "") if tag is None and resumed else _pick(tag, bound, unbound, sid)
    if event == "start":
        board.record_route(agent_id, sid, "final", job)
    elif not board.claim_route(agent_id, sid, route.state, "final", job):
        return  # a parallel tool call of the same agent decided first
    if job:
        _enrol(board, event, agent_id, job, payload, cfg, sid, bound.get(job) or unbound.get(job), prompt)
    else:
        _refuse(event, agent_id, why, started_here and tag is not None)


def _verify_route(board, agent_id: str, sid: str, member, bound: dict, unbound: dict,
                  payload: dict, cfg: dict):
    """First tool call of an agent that joined the session's only job at SubagentStart, before
    its tag was readable: keep it there unless the tag names another job. A verifier tag, now
    readable, makes it a verifier with its own instructions, before this very call runs.
    Returns the member to carry on with, or None when this call has been dealt with."""
    prompt = _spawn_prompt(payload, agent_id) or ""
    tag = _tag_of(prompt)
    if tag is None or tag == member.job:
        board.claim_route(agent_id, sid, "unverified", "final", member.job)
        from swarm.roles import custom_role
        role = custom_role(_role(payload, agent_id, prompt))
        if role:
            board.set_agent_role(agent_id, role)
        if _role(payload, agent_id, prompt) == "verifier" and board.claim_verifier(agent_id, member.job):
            js = board.job_status(member.job)
            _out("PreToolUse", "[swarm] Your prompt makes you a verifier: this replaces the worker "
                 "instructions you were given.\n" + _verifier_instructions(
                     member.name, member.job, cfg, js.goal if js else None, js.judge if js else None))
            from dataclasses import replace
            return replace(member, verifier=True)
        return member
    job, why = _pick(tag, bound, unbound, sid)
    if not board.claim_route(agent_id, sid, "unverified", "final", job):
        return None  # a parallel tool call of the same agent is handling it
    board.leave(agent_id)
    if job:
        _enrol(board, "turn", agent_id, job, payload, cfg, sid, bound.get(job) or unbound.get(job))
    else:
        _drop_enrolment(cfg, agent_id)
        _refuse("turn", agent_id, why, True)
    return None


# --------------------------------------------------------------------------- spawning
#
# Swarm agents have the Agent tool too, and the subagents they spawn join the board like any
# other (their transcripts sit in the same subagents/ directory). To keep fan-out bounded, a
# member's Agent call only goes ahead when it is justified and within the [spawn] caps; anything
# else is denied at PreToolUse with the reason. The depth comes from Claude Code's own
# agent-<id>.meta.json (spawnDepth: 1 for the orchestrator's agents); if it can't be read the
# spawn is refused. A granted spawn is counted before the child exists (a spawn that then fails
# still counts: the caps err on the strict side) and announced on the board with its reason.

def _spawn_depth(payload: dict, agent_id: str) -> int | None:
    return current_host().spawn_depth(payload, agent_id)


def _spawn_refusal(board, agent_id: str, member, payload: dict, cfg: dict) -> str | None:
    """None if this Agent call may go ahead (it is then counted and announced), else why not."""
    sp, job = cfg["spawn"], member.job
    per_agent, per_job = int(sp["max_per_agent"]), int(sp["max_per_job"])
    max_depth, min_chars = int(sp["max_depth"]), int(sp["min_justification_chars"])
    call = current_host().spawn_call(payload.get("tool_input") or {})
    prompt = call.prompt
    if per_job <= 0 or per_agent <= 0:
        return "spawning subagents is switched off for swarm agents"
    js = board.job_status(job)
    if js and js.judge == member.name and js.verdict != "not_met":
        return ("the judge spawns subagents only after it has recorded a not_met verdict (with --reason "
                "and --next): it spawns the fix agents with that brief")
    depth = _spawn_depth(payload, agent_id)
    if depth is None:
        return "your spawn depth can't be determined, so spawning is refused"
    if depth >= max_depth:
        return f"you are at spawn depth {depth} and the limit is {max_depth}: helpers can't spawn helpers"
    host = current_host()
    why = _tag_of(prompt, SPAWN_TAG) or ""
    if host.reads_prompt_tags:
        if len(why) < min_chars:
            return (f"the child's prompt needs a line `{SPAWN_TAG} <why it is strictly needed>]` of at "
                    f"least {min_chars} characters")
        if _tag_of(prompt) != job:
            return f"the child's prompt needs the line `{tag_line(job)}` so it joins this job's board"
    # else (Codex: the message is encrypted) only the caps and the depth can be checked; the
    # child joins this session's job anyway
    from swarm.roles import from_prompt
    if ((from_prompt(prompt) if host.reads_prompt_tags else None) or host.spawn_role_hint(call)) == "judge":
        return "a spawned subagent can't be a judge"
    grant = board.reserve_spawn(agent_id, job, per_agent, per_job)
    if grant.refused == "agent":
        return f"you have already spawned {grant.agent_spawns} (limit {per_agent} per agent)"
    if grant.refused == "job":
        return f"agents on job \"{job}\" have already spawned {grant.job_spawns} (limit {per_job} per job)"
    if not grant.granted:
        return "you are not an active member of this job"
    what = call.description
    board.post(job, member.name, f"spawning {what} ({grant.job_spawns}/{per_job} for the job)"
               + (f": {why}" if why else ""), agent_key=agent_id)
    return None


def _spawn_lines(cfg: dict, job: str) -> list[str]:
    sp = cfg["spawn"]
    if int(sp["max_per_job"]) <= 0 or int(sp["max_per_agent"]) <= 0:
        return ["- Don't spawn subagents: that is switched off for swarm agents. Ask on the board instead."]
    from swarm import models
    host = current_host()
    line = (f"- Spawning subagents: only when strictly needed, i.e. the work can't reasonably be done by "
            f"you or by an agent already on the board (ask there first). Limits: {sp['max_per_agent']} per "
            f"agent, {sp['max_per_job']} for the whole job, and no deeper than depth {sp['max_depth']} "
            f"(the orchestrator's agents are depth 1, their helpers depth 2, and so on). ")
    if host.reads_prompt_tags:
        line += (f"The child's prompt must contain the lines `{tag_line(job)}` and `{SPAWN_TAG} <why it "
                 f"is strictly needed>]` (at least {sp['min_justification_chars']} characters); the "
                 f"reason is posted on the board. Anything else is refused. Give the child a role "
                 f"with `[swarm role: engineer]`, for example; the brief defines its responsibilities.")
    else:   # Codex: the spawn message is encrypted, only its task_name is readable
        line += ("Your child joins this job by itself, and its spawn is announced on the board: say "
                 "there why it is needed. Use task_name `<role>__<task>` (e.g. `engineer__api`) "
                 "to assign a role. `verifier` is read-only and `judge` is refused; legacy "
                 "verifier/judge prefixes without `__` still work. The brief defines custom responsibilities.")
    hint = None if host.supports_spawn_model_rewrite else models.model_for(cfg, host.name, "helper")
    if hint:
        line += f" Spawn helpers with model `{hint}`."
    return [line]


def _memory_on(cfg: dict) -> bool:
    return bool(str((cfg.get("hindsight") or {}).get("url") or "").strip())


def _post_cmd(job: str, name: str, to: str | None = None) -> str:
    to_part = f" --to {_q(to)}" if to else ""
    return f"{_bin()} post --job {_q(job)} --as {_q(name)}{to_part} \"<message>\""


def _instructions(name: str, job: str, cfg: dict, goal: str | None = None, judge: str | None = None) -> str:
    cap = cfg["board"]["message_max_chars"]
    lines = [
        f"[swarm] You are **{name}**, a member of the swarm working on job \"{job}\". "
        f"Other agents on this job share a message board with you.",
    ]
    if goal:
        who = f"The judge, {judge}," if judge else "The judge (shown as (judge) in the roster once it joins)"
        until = judge or "the judge"
        lines.append(f"- Goal: {goal}\n  {who} decides whether it is met: the job is not done until "
                     f"{until} records the verdict met. Give the judge proof when asked, and fix what "
                     f"a not_met verdict names.")
    lines.append("- When you finish something another agent could check, post `DONE: <what, and how "
                 "to check it>`. Verifiers (shown as (verifier) in the roster) check such claims "
                 "and answer VERIFIED or FAILED with evidence: fix what fails.")
    lines.append(f"- Before you end your turn to wait for background work that will wake you (a "
                 f"monitor, a long remote run, a lock), run `{_bin()} wait --job {_q(job)} --on "
                 f"\"<what you wait for>\"`: a job whose agents have all finished auto-closes "
                 f"after a quiet spell otherwise. Once it is over: `{_bin()} resume --job {_q(job)}`.")
    lines += _board_lines(name, job, cap, cfg)
    lines += _spawn_lines(cfg, job)
    return "\n".join(lines)


def _verifier_instructions(name: str, job: str, cfg: dict, goal: str | None = None,
                           judge: str | None = None) -> str:
    cap = cfg["board"]["message_max_chars"]
    lines = [
        f"[swarm] You are **{name}**, a VERIFIER on job \"{job}\". You check the other agents' "
        f"work independently. You don't do it and you don't fix it.",
        "- Check what agents claim is done: their `DONE:` posts, their results and anything your "
        "own brief names. Look at the actual result and re-run the checks yourself; a claim or "
        "a description of a check is not evidence.",
        f"- Post each outcome, addressed to the agent that made the claim: "
        f"`{_post_cmd(job, name, '<agent>')}` with `VERIFIED: <claim>` or `FAILED: <claim>: "
        f"<evidence>`. Status counts these posts, so start them exactly that way.",
        "- You are read-only: Edit, Write and spawning subagents are refused for you. Don't change "
        "anything through Bash either: no writes, restarts, installs or config changes. If a check "
        "needs something changed, ask its owner on the board.",
    ]
    if goal:
        lines.append(f"- The job's goal: {goal}\n  " + (f"{judge} is" if judge else "The judge is")
                     + " the one who rules on it; your VERIFIED/FAILED posts are evidence for the judge.")
    lines += _board_lines(name, job, cap, cfg)
    return "\n".join(lines)


def _judge_instructions(name: str, job: str, goal: str, cfg: dict) -> str:
    cap = cfg["board"]["message_max_chars"]
    met = f"{_bin()} verdict --job {_q(job)} --as {_q(name)} met \"<short reason>\""
    not_met = (f"{_bin()} verdict --job {_q(job)} --as {_q(name)} not_met --reason \"<why it is not met>\" "
               f"--next \"<what to change, where, and what you will re-check>\"")
    lines = [
        f"[swarm] You are **{name}**, the JUDGE of job \"{job}\". Your only job is to decide whether "
        f"its goal has been met. You don't do the work.",
        f"Goal: {goal}",
        "- Gather evidence: read what the workers post, inspect their results yourself and run "
        "your own checks. Ask workers for proof with --to '<name>', and request fixes with --to "
        "when something falls short.",
        "- Judge strictly against the goal text, including anything it implies that the workers "
        "skipped. Claims without evidence don't count.",
        f"- When you are confident, record your verdict: `{met}`, or, if the goal is not met, "
        f"`{not_met}`. A not_met verdict is REFUSED without both --reason (why you ruled so) and "
        f"--next (concrete instructions: what to change, where, and what you will re-check). Those "
        f"instructions become the brief the fix agents are spawned with, so make them complete on "
        f"their own. It is broadcast and shown by `swarm status --job`; judge again once the "
        f"workers have fixed it. The job can't be completed until your verdict is met.",
        "- Only after you have recorded a not_met verdict may you spawn subagents: spawn the fix agents "
        "yourself, each given your --next brief (plus its own part of it), and as many extra agents "
        "as the work needs, within the spawn rules below. Never spawn a judge. Before a not_met "
        "verdict, and after met, your spawns are refused: judging is yours alone.",
        "- Verifiers, if the job has any, post VERIFIED/FAILED results: use them as evidence, but "
        "check anything the verdict rests on yourself.",
    ]
    lines += _spawn_lines(cfg, job)
    lines += _board_lines(name, job, cap, cfg)
    return "\n".join(lines)


def _board_lines(name: str, job: str, cap: int, cfg: dict) -> list[str]:
    """How to use the board: the same for workers and the judge."""
    lines = [
        f"- Post: `{_post_cmd(job, name)}` (add `--to '<agent name>'` to address someone). "
        f"Max {cap} characters, plain text, no secrets.",
        "- Use the board actively. Broadcast (no --to) claims before you touch anything shared, "
        "findings, warnings, blockers and results. Use --to '<exact name>' for questions, "
        "requests, handoffs and answers; pick the recipient from the roster. Always reply to "
        "messages addressed to you and acknowledge requests. If another agent owns something, "
        "ask its owner on the board instead of doing it yourself. Post a short status every few "
        "steps and a final one when you are done.",
        "- New messages from other agents are shown to you automatically before your tool calls, "
        "prefixed `[swarm board]`. Act on anything addressed to you or touching what you are doing.",
        "- Who else is on the job is shown prefixed `[swarm roster]`: in full now and "
        "periodically, and as a short list of changes whenever someone joins, leaves, finishes "
        f"or goes quiet. Full list any time: `{_bin()} who --job {_q(job)}`. "
        f"Your name is fixed; always post as {name}.",
    ]
    if current_host().name == "codex":   # no network in the Codex sandbox
        lines.append(
            "- If your sandbox can't reach the board (no network), `post`, `wait` and `resume` "
            "say \"queued\": that is fine, the hooks deliver them within seconds. `who` and "
            "`read` need the board and fail there: rely on the `[swarm board]` and "
            "`[swarm roster]` lines instead.")
    if _memory_on(cfg):
        lines.append(
            f"- Project memory: whenever you learn something durable (a finding, root cause, "
            f"decision or gotcha that would help a later agent on this project), store it: "
            f"`{_bin()} remember --job {_q(job)} --as {_q(name)} \"<fact>\"`. One self-contained fact "
            f"per call; never secrets, never narration of what you are doing. Relevant memories "
            f"are shown to you prefixed `[swarm memory]`.")
    return lines


# --------------------------------------------------------------------------- roster

def _state_word(status: str) -> str:
    """What the roster tracks: started/running collapse to "active" (tool changes are not news)."""
    return "active" if status in ACTIVE_STATUSES else status


def roster_snapshot(roster, me: str) -> str:
    """The roster as the agent `me` knows it, stored in its sync state: [key, name, role, state]."""
    return json.dumps([[e.agent_key, e.name, e.role, _state_word(e.status)]
                       for e in roster if e.agent_key != me], separators=(",", ":"))


def _t(s) -> str:
    """Board data as one line of agent context (textsafe.term_safe: no newline, escape or bidi
    control can start a line that looks like the harness's own)."""
    from swarm.textsafe import term_safe
    return term_safe(s)


def roster_changes(before: str | None, after: str) -> list[str]:
    """Short diff lines between two snapshots: "joined: X (role)", "completed: Y", "back: Z"."""
    old = {k: state for k, _, _, state in json.loads(before or "[]")}
    out = []
    for key, name, role, state in json.loads(after):
        was = old.get(key)
        if was == state:
            continue
        name, role, state = _t(name), role and _t(role), _t(state)
        if was is None and state == "active":
            out.append(f"joined: {name}" + (f" ({role})" if role else ""))
        else:
            out.append(f"back: {name}" if state == "active" else f"{state}: {name}")
    return out


def roster_text(roster, me: str, job: str) -> str:
    others = [e for e in roster if e.agent_key != me]
    if not others:
        return f"[swarm roster] you are the only agent on job \"{job}\" so far."
    active = [e for e in others if e.active]
    lines = [f"[swarm roster] other agents on job \"{job}\" ({len(active)} active; "
             f"address them with --to '<exact name>'):"]
    for e in active:
        tool = f", in {_t(e.current_tool)}" if e.current_tool and e.status == "running" else ""
        lines.append(f"- {_t(e.name)}" + (f" ({_t(e.role)})" if e.role else "") + f": {_t(e.status)}{tool}")
    gone = [e for e in others if not e.active]
    if gone:
        lines.append("finished: " + ", ".join(f"{_t(e.name)} ({_t(e.status)})" for e in gone))
    return "\n".join(lines)


def _roster_update(board, agent_id: str, job: str, roster, state, cfg: dict) -> str | None:
    """The roster news for this turn (full roster when due, else a diff, else None); records
    what was shown in the agent's sync state."""
    snap = roster_snapshot(roster, agent_id)
    changes = roster_changes(state.roster_seen, snap)
    refresh = float(cfg["board"].get("roster_refresh_minutes", 10)) * 60
    full = state.roster_synced_at is None or (state.now - state.roster_synced_at).total_seconds() >= refresh
    text = None
    if full:
        text = roster_text(roster, agent_id, job)
        if changes:
            text = "[swarm roster] changes: " + "; ".join(changes) + "\n" + text
    elif changes:
        text = "[swarm roster] changes: " + "; ".join(changes)
    if text or snap != state.roster_seen:
        board.record_roster_sync(agent_id, snap, full)
    return text


# --------------------------------------------------------------------------- messages

def _messages_text(res, job: str, me: str, heading: str) -> str | None:
    if not res.messages:
        return None
    parts = [f"[swarm board] {heading}:\n" + fmt(res.messages)]
    senders = list(dict.fromkeys(_t(m.agent_name) for m in res.messages if m.to_agent == me))
    if senders:
        n = sum(1 for m in res.messages if m.to_agent == me)
        parts.append(f"[swarm board] {n} addressed to you: reply with `{_post_cmd(job, me, senders[0])}`"
                     + (f" (also from: {', '.join(senders[1:])})" if senders[1:] else ""))
    if res.remaining:
        s = "" if res.remaining == 1 else "s"
        parts.append(f"[swarm board] {res.remaining} more unread message{s}: they are shown before "
                     f"your next tool calls, oldest first.")
    return "\n".join(parts)


def _reply_reminders(board, agent_id: str, job: str, name: str, state, delivered) -> str | None:
    """Messages addressed to the agent that it was shown on an earlier call and hasn't answered
    (no --to reply to the sender since): one reminder per sender, each message only once."""
    owed = [o for o in state.replies_owed if o.id not in delivered]
    if not owed:
        return None
    latest = {o.sender: o for o in owed}   # by id order: the newest per sender wins
    board.record_reply_reminder(agent_id, max(o.id for o in owed))
    return "\n".join(f"[swarm] {_t(o.sender)} asked you something at "
                     f"{o.created_at.astimezone().strftime('%H:%M')}: reply with "
                     f"`{_post_cmd(job, name, _t(o.sender))}`" for o in latest.values())


def _silence_nudge(board, agent_id: str, job: str, name: str, state, cfg: dict) -> str | None:
    """Once per quiet window: the agent made silence_nudge_calls tool calls, or spent
    silence_nudge_minutes, without posting. The window restarts when it posts (0 disables)."""
    b = cfg["board"]
    max_calls, max_minutes = int(b.get("silence_nudge_calls", 15)), float(b.get("silence_nudge_minutes", 10))
    quiet_since = state.last_post_at or state.joined_at
    if state.silence_nudged_at is not None and state.silence_nudged_at >= quiet_since:
        return None
    calls = state.tool_calls - state.calls_at_post
    minutes = (state.now - quiet_since).total_seconds() / 60
    if max_calls and calls >= max_calls:
        how = f"{calls} tool calls"
    elif max_minutes and minutes >= max_minutes:
        how = f"{int(minutes)} minutes"
    else:
        return None
    board.record_silence_nudge(agent_id)
    return (f"[swarm] status? You have not posted for {how}. Post a short status for the others "
            f"(what you are doing, what you found, what you need): `{_post_cmd(job, name)}`")


# --------------------------------------------------------------------------- memory (Hindsight)

def _minutes_since(state, ts) -> float:
    return float("inf") if ts is None else (state.now - ts).total_seconds() / 60


HOOK_RECALL_SECONDS = 2.0   # a periodic (mid-work) recall, all of it, takes at most this long
HOOK_RECALL_ENV = "SWARM_HOOK_RECALL_SECONDS"   # overrides [hindsight] recall_start_seconds (tests, debugging)


def _start_recall_seconds(cfg: dict) -> float:
    """How long the recall made when an agent joins may take: the first recall after a long idle
    is cold (8-10 s on a local Hindsight with a reranker), so it gets [hindsight]
    recall_start_seconds (default 12); the env var wins over the config."""
    for raw in (os.environ.get(HOOK_RECALL_ENV), cfg["hindsight"].get("recall_start_seconds")):
        try:
            if raw not in (None, "") and float(raw) > 0:
                return float(raw)
        except (TypeError, ValueError):
            pass
    return 12.0


def _recall(board, cfg: dict, agent_id: str, job: str, seen, heading: str, *, start: bool = False) -> str | None:
    """Recall the project's memories, show the ones this agent hasn't seen, record them.
    Hindsight trouble is logged and skipped (the recall still counts, so it isn't retried
    before recall_minutes), except a start recall that ran out of time: that one is not
    recorded, so the agent's next turn tries again (the cold Hindsight is warm by then)."""
    from swarm import hindsight
    js = board.job_status(job)
    project = hindsight.project_of(js, job)
    # one deadline for the whole call, name resolution included (hindsight.Client): a hook never
    # hangs an agent on Hindsight
    seconds = _start_recall_seconds(cfg) if start else min(HOOK_RECALL_SECONDS, _start_recall_seconds(cfg))
    bounded = {**cfg, "hindsight": {**cfg["hindsight"], "deadline": time.monotonic() + seconds}}
    try:
        items = hindsight.Client(bounded).recall(hindsight.bank_id(project), hindsight.recall_query(js, job))
    except Exception as exc:
        _log_error("memory", agent_id, exc)
        if not (start and isinstance(exc, hindsight.HindsightOutOfTime)):
            board.record_memory_recall(agent_id, [])
        return None
    text, shown = hindsight.format_memories([i for i in items if i["id"] not in seen], project, cfg, heading)
    board.record_memory_recall(agent_id, shown)
    return text


def _memory_turn(board, cfg: dict, agent_id: str, job: str, state) -> list[str]:
    """Periodic recall (new memories only) and the "store what you learned" reminder."""
    if not _memory_on(cfg) or state is None:
        return []
    h = cfg["hindsight"]
    out = []
    if _minutes_since(state, state.memory_recalled_at) >= float(h["recall_minutes"]):
        text = _recall(board, cfg, agent_id, job, set(state.memory_seen), "new memories for this project")
        if text:
            out.append(text)
    quiet_since = max(t for t in (state.remembered_at, state.nudged_at, state.joined_at) if t is not None)
    if _minutes_since(state, quiet_since) >= float(h["remember_nudge_minutes"]):
        board.record_nudge(agent_id)
        out.append(f"[swarm memory] reminder: you have stored nothing in project memory for "
                   f"{int(h['remember_nudge_minutes'])} minutes. If you learned something durable "
                   f"(a finding, root cause, decision), store it now: `{_bin()} remember --job {_q(job)} "
                   f"--as {_q(state.name)} \"<fact>\"`. Skip it if nothing qualifies.")
    return out


# --------------------------------------------------------------------------- events

_OUTPUT: dict = {}   # what this hook invocation answers; run_hook prints it once, at the end


def _out(event: str, text: str) -> None:
    o = _OUTPUT.setdefault("hookSpecificOutput", {"hookEventName": event})
    o["additionalContext"] = f"{o['additionalContext']}\n\n{text}" if o.get("additionalContext") else text


def _deny(reason: str) -> None:
    """Refuse the tool call (PreToolUse only); the agent is shown the reason. Drops any input
    rewrite set earlier in this invocation (and its "allow"): never a deny together with
    updatedInput."""
    o = _OUTPUT.setdefault("hookSpecificOutput", {"hookEventName": "PreToolUse"})
    o.pop("updatedInput", None)
    o["permissionDecision"], o["permissionDecisionReason"] = "deny", reason


def _set_input_rewrite(fields: dict) -> None:
    """Merge the host's input-rewrite fields (Host.input_rewrite_output) into the output, unless
    the call is already denied."""
    o = _OUTPUT.setdefault("hookSpecificOutput", {"hookEventName": "PreToolUse"})
    if o.get("permissionDecision") == "deny":
        return
    o.update(fields)


def _apply_model(cfg: dict, payload: dict, role: str, *, fallback: str = "worker") -> None:
    """Set the spawn's model to the role's ([models]), where the host lets a hook rewrite it."""
    from swarm import models
    host = current_host()
    ti = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    model = models.choose(cfg, host.name, role, host.spawn_call(ti).model, fallback=fallback)
    if model and host.supports_spawn_model_rewrite:
        _set_input_rewrite(host.input_rewrite_output(host.rewrite_spawn_model(ti, model)))


def _orchestrator_spawn(cfg: dict, sid: str | None, payload: dict) -> None:
    """The orchestrator's own spawn (no agent_id): the role's model, if the child will join a
    job, the way _pick routes it: its tag names a job bound to this session or claimable by it,
    or it has no tag and there is exactly one such job (the bound ones, else the claimable).
    Reads markers only, opens no board, claims nothing. The role: its [swarm role:] tag, or on
    Codex the task name."""
    from swarm import models
    if not sid:
        return
    bound, unbound = _session_markers(cfg, sid)
    host = current_host()
    call = host.spawn_call(payload.get("tool_input") or {})
    tag = _tag_of(call.prompt)
    if (tag is not None and (tag in bound or tag in unbound)) or (tag is None and len(bound or unbound) == 1):
        prompt = call.prompt if host.reads_prompt_tags else ""
        _apply_model(cfg, payload, models.role_of(prompt, False, host.spawn_role_hint(call)))


def _gate_spawn(board, agent_id: str, name: str, job: str, payload: dict, cfg: dict) -> bool:
    """For a member's Agent call: refuse it unless allowed (see "spawning"). True = go ahead."""
    if not current_host().is_spawn(payload):
        return True
    from swarm.board import Member
    why = _spawn_refusal(board, agent_id, Member(name, job), payload, cfg)
    if why is None:
        from swarm import models
        host = current_host()
        call = host.spawn_call(payload.get("tool_input") or {})
        prompt = call.prompt if host.reads_prompt_tags else ""
        role = models.role_of(prompt, True, host.spawn_role_hint(call))
        _apply_model(cfg, payload, role, fallback="helper")
        return True
    board.tool_finished(agent_id)   # a denied call gets no PostToolUse
    _deny(f"[swarm] Spawn refused: {why}. Spawn only when strictly needed: do the work yourself, "
          f"or ask on the board for an agent already on the job to take it.")
    return False


def _gate_verifier(board, agent_id: str, is_verifier: bool, payload: dict) -> bool:
    """A verifier's writing tools and spawns are refused, and so are shell commands that look like
    they write (best effort, swarm.shellguard). True = go ahead."""
    if not is_verifier:
        return True
    host = current_host()
    if host.denies_verifier(payload):
        why = "verifiers are read-only (no Edit, Write, apply_patch or spawning)"
    else:
        from swarm.shellguard import writes_files
        cmd = host.shell_command(payload)
        hit = writes_files(cmd) if cmd else None
        if hit is None:
            return True
        why = f"this shell command looks like it writes files ({hit}), and verifiers are read-only"
    board.tool_finished(agent_id)   # a denied call gets no PostToolUse
    _deny(f"[swarm] Refused: {why}. Check, don't fix: post FAILED with the evidence, --to the agent "
          f"whose work it is.")
    return False


def _gate_new_member(board, agent_id: str, bound: dict, payload: dict, cfg: dict) -> None:
    """The verifier and spawn gates for an agent that (re)joined a board on this very tool call."""
    if not current_host().denies_verifier(payload) and not current_host().shell_command(payload):
        return
    name, job = board.active_agent_name(agent_id), board.route(agent_id).member_job
    if not (name and job in bound):
        return
    verifier = any(a.agent_key == agent_id and a.role == "verifier"
                   for a in board.agents(job, include_departed=False))
    if _gate_verifier(board, agent_id, verifier, payload):
        _gate_spawn(board, agent_id, name, job, payload, cfg)


def _append_host_log(name: str, line: str) -> None:
    """Append one line to `name` in the host-only dir (paths.host_dir(): 0700, outside every
    sandbox writable root), made one printable line first (safefs.log_safe: payload, marker and
    board data can hold newlines). Through safefs: a planted symlink, hard link or FIFO is
    refused, never followed or waited on. Never raises."""
    try:
        from swarm import paths, safefs
        with safefs.dir_fd(paths.host_dir(), strict_mode=0o700) as d:
            safefs.append(d, name, safefs.log_safe(line) + "\n")
    except Exception:
        pass


def _log_line(event: str, agent_id: str, text: str) -> None:
    """Append one line to hook-errors.log (in the host-only dir); never raises."""
    _append_host_log("hook-errors.log", f"{time.strftime('%F %T')} {event} {agent_id}: {text}")


def _log_error(event: str, agent_id: str, exc: Exception) -> None:
    """Leave a trace for debugging; never raises."""
    # BoardUnavailable wraps the driver error; log the driver's type as before the port.
    err = exc.__cause__ if type(exc).__name__ == "BoardUnavailable" and exc.__cause__ else exc
    _log_line(event, agent_id, f"{type(err).__name__}: {exc}")


HOOK_INIT_TIMEOUT = 4.0   # seconds a hook waits for another process's board setup
# Spool delivery per hook: (records at most, seconds in all, seconds per delivery). The per-tool
# hooks deliver a couple at most, each bounded to fit; the backlog goes out from the Start/Stop
# hooks (and the CLI, unbounded). Memories (Hindsight HTTP) go out only from Start/Stop.
HOOK_FLUSH_TOOL = (2, 1.0, 1.0)     # PreToolUse, PostToolUse: board posts only
HOOK_FLUSH_AGENT = (20, 2.0, 1.0)   # SubagentStart, SubagentStop


def _auto_init(event: str, agent_id: str, cfg: dict) -> None:
    """Set the board up if it isn't for this code (board.ensure_initialized; a stamp check once
    done), within the hook's deadline. Schema only: a hook never registers hooks. Anything
    noteworthy or failing goes to hook-errors.log and the hook carries on."""
    try:
        from swarm.board import SCHEMA_VERSION, ensure_initialized
        res = ensure_initialized(cfg, timeout=HOOK_INIT_TIMEOUT)
        if res.action == "initialized":
            _log_line(event, agent_id, f"auto-init: board set up (schema version {SCHEMA_VERSION})")
        elif res.action == "newer":
            _log_line(event, agent_id, f"auto-init: warning: the board has schema version "
                                       f"{res.version}, newer than this code's {SCHEMA_VERSION}; "
                                       f"left untouched")
    except Exception as exc:
        _log_error(event, agent_id, exc)


def _goal_roles(board, agent_id: str, job: str, payload: dict, marker: dict | None,
                prompt: str | None) -> tuple[str | None, str | None, bool, str | None]:
    """For a job with a goal: (goal, judge name, whether this agent is the judge, a note if its
    judge tag was refused). (None, None, False, None) for a job without one."""
    if not (marker or {}).get("goal"):
        return None, None, False, None
    if prompt is None:
        prompt = _spawn_prompt(payload, agent_id)
    wants = _role(payload, agent_id, prompt) == "judge"
    is_judge = wants and board.claim_judge(agent_id, job)
    js = board.job_status(job)
    goal, judge = (js.goal, js.judge) if js else (None, None)
    note = None
    if wants and not is_judge:
        note = (f"[swarm] Your prompt makes you a judge, but {judge or 'another agent'} is already "
                f"the judge of job \"{job}\" (one per job). You are on the board as a worker: your "
                f"verdicts would be refused. Say so in your report.")
    return goal, judge, is_judge, note


# --------------------------------------------------------------------------- enrolment records
#
# The supervisor launches replacements (unsandboxed) only for agents this host enrolled itself
# (a launch must not rest on board rows an agent can write): the record, in the host-only dir, is written here, by the hook, which
# runs outside every sandbox. Its harness, session and cwd come from this hook invocation (the
# host's payload, the hook process), never from the board or a marker's contents: a marker only
# says which job, and _markers has re-validated its name.

def _payload_cwd(payload: dict) -> str:
    """The session's working directory as the host reports it, if it is a plain absolute path;
    else the hook process's own."""
    from swarm.textsafe import has_controls
    c = payload.get("cwd")
    if (isinstance(c, str) and 0 < len(c) <= 4096 and os.path.isabs(c) and os.path.normpath(c) == c
            and ".." not in c.replace(os.sep, "/").split("/") and not has_controls(c)):
        return c
    return os.getcwd()


def _record_enrolment(cfg: dict, agent_id: str, job: str, sid: str | None, payload: dict) -> None:
    """Write (or replace) the local enrolment record of agent_id on job. Never raises."""
    try:
        from swarm import enrolment
        from swarm.board.autoinit import store_key
        enrolment.write(store_key(cfg), job=job, agent_key=agent_id, harness=current_host().name,
                        session_id=sid, cwd=_payload_cwd(payload))
    except Exception as exc:
        _log_error("enrol", agent_id, exc)


def _drop_enrolment(cfg: dict, agent_id: str) -> None:
    try:
        from swarm import enrolment
        from swarm.board.autoinit import store_key
        enrolment.remove(store_key(cfg), agent_id)
    except Exception as exc:
        _log_error("enrol", agent_id, exc)


def _record_activations(cfg: dict, sid: str | None, payload: dict, markers: list[dict]) -> None:
    """An orchestrator's hook call: record each job activated for this session (a marker bound
    to it) in a local job record, the start of its transcript snapshot window. Written at the
    first hook call after the activation and left alone after, unless the marker is newer
    than the record (a re-activation). Never raises."""
    if not sid:
        return
    try:
        from swarm import enrolment
        from swarm.board.autoinit import store_key
        key = None
        for m in markers:
            if m.get("session_id") != sid or "resume" in m:
                continue
            key = key or store_key(cfg)
            rec = enrolment.find_job(key, m["job"])
            if rec is not None and rec.session_id == sid and rec.created_at >= (m.get("_mtime") or 0):
                continue
            enrolment.write_job(key, job=m["job"], harness=current_host().name, session_id=sid,
                                cwd=_payload_cwd(payload))
    except Exception as exc:
        _log_error("enrol", "main", exc)


def _enrol(board, event: str, agent_id: str, job: str, payload: dict, cfg: dict, sid: str | None,
           marker: dict | None = None, prompt: str | None = None) -> None:
    """Name an agent the board has no active row for and hand it the board instructions (the
    judge's, if it is the judge of a job with a goal), the roster, what was posted before it
    (re)joined and, with Hindsight, the project's memories. Routing (_route_new) has already
    decided that it belongs on `job`; `marker` is that job's marker, `prompt` the spawn prompt
    if it was read already."""
    returning = board.was_member(agent_id, job)
    if prompt is None:
        prompt = _spawn_prompt(payload, agent_id)
    from swarm.roles import custom_role
    role = custom_role(_role(payload, agent_id, prompt))
    name = board.allocate_name(agent_id, job, role or payload.get("agent_type"))
    if role:
        board.set_agent_role(agent_id, role)
    _record_enrolment(cfg, agent_id, job, sid, payload)
    board.set_waiting(job, None)   # an agent at work: the job is no longer waiting for anything
    if sid:
        board.bind_job_session(job, sid)
    if event == "turn":
        board.tool_started(agent_id, payload.get("tool_name"))
    goal, judge, is_judge, note = _goal_roles(board, agent_id, job, payload, marker, prompt)
    if prompt is None:
        prompt = _spawn_prompt(payload, agent_id)   # None at SubagentStart: see _verify_route
    is_verifier = (not is_judge and _role(payload, agent_id, prompt) == "verifier"
                   and board.claim_verifier(agent_id, job))
    if is_verifier and goal is None:
        js = board.job_status(job)
        goal, judge = (js.goal, js.judge) if js else (None, None)
    heading = "messages since you left" if returning else "recent messages on this job (before you joined)"
    _welcome(board, event, agent_id, job, name, cfg, is_judge=is_judge, is_verifier=is_verifier,
             goal=goal, judge=judge, note=note, heading=heading, payload=payload)


def _welcome(board, event: str, agent_id: str, job: str, name: str, cfg: dict, *, is_judge: bool,
             is_verifier: bool, goal, judge, note, heading: str, payload: dict) -> None:
    """The board instructions (judge's, verifier's or worker's), the roster, the unread messages
    under `heading`, memories, the runtime, then the output: what a (re)joining agent is shown."""
    parts = [_judge_instructions(name, job, goal, cfg) if is_judge
             else _verifier_instructions(name, job, cfg, goal, judge) if is_verifier
             else _instructions(name, job, cfg, goal, judge), note]
    roster = board.roster(job)
    board.record_roster_sync(agent_id, roster_snapshot(roster, agent_id), True)
    parts.append(roster_text(roster, agent_id, job))
    parts.append(_messages_text(board.read_unread(agent_key=agent_id, job=job), job, name, heading))
    if _memory_on(cfg):
        parts.append(_recall(board, cfg, agent_id, job, (), "what this project's memory knows",
                              start=True))
    host = current_host()
    board.set_agent_runtime(agent_id, host.name, payload.get("model") or host.agent_model(payload, agent_id))
    _out("SubagentStart" if event == "start" else "PreToolUse", "\n\n".join(p for p in parts if p))


# --------------------------------------------------------------------------- replacements
#
# A supervisor replacement is a root session, so its hook calls carry no agent_id:
# the resume marker bound to its session id makes it the member resume.agent_key. Its first
# tool call enrols it under its predecessor's name (Board.claim_resume); it may not spawn.
# The stuck-closed original, if it comes back while its replacement is active, is stopped.

def _replacement_token() -> str | None:
    from swarm.supervisor.markers import TOKEN_ENV
    return os.environ.get(TOKEN_ENV) or None


def _resume_binding(cfg: dict, sid: str | None) -> dict | None:
    """The resume marker bound to this root session (a supervisor replacement), or
    None. Files only; never raises."""
    if not sid:
        return None
    try:
        from swarm.supervisor.markers import TOKEN_ENV, resume_binding, resume_by_token
        found = resume_binding(cfg, sid)
        if found is None and os.environ.get(TOKEN_ENV):
            # a Codex replacement whose runner hasn't bound its thread id yet: bind by its token
            found = resume_by_token(cfg, os.environ[TOKEN_ENV], sid)
        return found
    except Exception as exc:
        _log_error("resume", "main", exc)
        return None


def _enrol_resumed(board, agent_id: str, resume: dict, payload: dict, cfg: dict) -> None:
    """A replacement's first tool call: take its predecessor's name and hand it the board
    instructions with what was posted since the predecessor stopped; a spawn is denied even
    then. If the predecessor is back (claim_resume refuses), the replacement is told to
    stop."""
    r, job = resume["resume"], resume["job"]
    why = _resume_problem(board, agent_id, resume)
    if why:
        _log_error("resume", agent_id, RuntimeError(f"resume marker {resume.get('_path')} refused: {why}"))
        _deny(f"[swarm] This session is not a restart the swarm supervisor started ({why}). Stop now: "
              f"end your turn without further tool calls.")
        return
    from swarm.pause import PAUSE_RESUME_REASON
    row = next((x for x in board.restarts(job=job) if x.id == r.get("restart_id")), None)
    from_pause = row is not None and (row.reason or "").startswith(PAUSE_RESUME_REASON)
    off = None if from_pause else _supervise_off(board, cfg, job)   # a resume the user asked for is not the supervisor's
    if off:   # the kill switches hold at the enrolment too
        _deny(f"[swarm] The swarm supervisor is switched off ({off}), so this restart won't go "
              f"ahead. Stop now: end your turn without further tool calls.")
        return
    name = board.claim_resume(agent_id, r.get("resume_of") or "", job)
    if name is None:
        _deny(f"[swarm] {r.get('name') or 'The agent you replace'} is back on the board, so this "
              f"restart isn't needed. Stop now: end your turn without further tool calls.")
        return
    _record_enrolment(cfg, agent_id, job, payload.get("session_id"), payload)
    board.set_waiting(job, None)
    board.tool_started(agent_id, payload.get("tool_name"))
    js = board.job_status(job)
    me = next((a for a in board.agents(job, include_departed=False) if a.agent_key == agent_id), None)
    role = me.role if me else None
    goal, judge = (js.goal, js.judge) if js else (None, None)
    note = (f"[swarm] The swarm supervisor restarted you to continue the work of {name}, which "
            f"stopped responding: you are {name} now. You can't spawn subagents.")
    if from_pause:
        note = (f"[swarm] Job \"{job}\" was paused and resumed: you are {name} again, continuing from your "
                f"stored transcript. You can't spawn subagents.")
    _welcome(board, "turn", agent_id, job, name, cfg, is_judge=role == "judge",
             is_verifier=role == "verifier", goal=goal, judge=judge, note=note,
             heading=f"messages since {name} stopped", payload=payload)
    if current_host().is_spawn(payload):   # not even on its first call
        _deny_replacement_spawn(board, agent_id)


def _first_call_of_resumed(board, agent_id: str, job: str) -> bool:
    """Whether this tool call is the first of a session `swarm resume` started: its seat (a row with
    resume_of) was claimed up front, so it is a member already, but it has not been welcomed yet."""
    me = next((a for a in board.agents(job, include_departed=False) if a.agent_key == agent_id), None)
    return me is not None and me.resume_of is not None and me.tool_calls <= 1


def _supervise_off(board, cfg: dict, job: str) -> str | None:
    """Why no replacement may join now (a kill switch: [supervise] enabled, supervise.off, the
    job's --no-supervise), or None."""
    from swarm.supervisor.settings import off_reason
    why = off_reason(cfg)
    if why:
        return why
    js = board.job_status(job)
    if js is not None and not js.supervise:
        return f"supervise is off for job {job}"
    return None


def _resume_problem(board, agent_id: str, resume: dict) -> str | None:
    """Why a resume marker (sandbox-writable) must not make this session a
    replacement, or None. It must be backed by the board: an open restart row with the marker's
    restart id, on its job, replacing its resume_of, started by this host and OS user, and not
    bound to another agent."""
    import getpass
    r, job = resume.get("resume"), resume.get("job")
    if not isinstance(r, dict) or not isinstance(job, str):
        return "a malformed resume marker"
    rid, old = r.get("restart_id"), r.get("resume_of")
    if not isinstance(rid, int) or isinstance(rid, bool) or not isinstance(old, str):
        return "a malformed resume marker"
    row = next((x for x in board.restarts(job=job) if x.id == rid), None)
    if row is None or row.old_agent_key != old:
        return "no such restart"
    if row.ended_at is not None:
        return "that restart has ended"
    if row.host != compat.node() or row.os_user != getpass.getuser():
        return "another host's or user's restart"
    if row.new_agent_key not in (None, agent_id):
        return "that restart belongs to another session"
    return None


def _deny_replacement_spawn(board, agent_id: str) -> None:
    board.tool_finished(agent_id)   # a denied call gets no PostToolUse
    _deny("[swarm] A restarted agent can't spawn subagents: do the work yourself.")


def _paused_stop(board, agent_id: str) -> str | None:
    """The stop message for an agent that a `swarm pause` closed, while its job is still paused
    (swarm.pause.paused_stop_text), else None. Once the job is resumed the agent is no longer
    refused here: a resumed one has been replaced (the replaced-key check stops its old process),
    one that was not resumed is simply let back in by allocate_name."""
    from swarm.board import LEFT_PAUSED
    from swarm.pause import paused_stop_text
    job = board.route(agent_id).member_job
    if not job:
        return None
    me = next((a for a in board.agents(job) if a.agent_key == agent_id), None)
    if me is None or me.left_reason != LEFT_PAUSED or board.job_state(job) != "paused":
        return None
    rec = board.open_pause(job)
    return paused_stop_text(job, rec.paused_by if rec else None, rec.reason if rec else None)


def _stuck_closed(board, agent_id: str, replaced: bool = False) -> str | None:
    """The stop message for an agent the supervisor closed as stuck (a departed row with
    left_reason stuck:*, or a key a restart row names as the lost agent), else None. Such a key
    never comes back, however its replacement is doing (running, finished, or not started
    yet): allocate_name won't revive it either. replaced=True: the caller already knows a
    restart row names it, so it is stopped even if its row is active (revived before the fix)."""
    from swarm.board import STUCK_PREFIX
    job = board.route(agent_id).member_job
    if not job:
        return None
    rows = board.agents(job)
    me = next((a for a in rows if a.agent_key == agent_id), None)
    if me is None:
        return None
    if not replaced:
        if me.ended_at is None:
            return None
        if not (me.left_reason or "").startswith(STUCK_PREFIX) and not board.was_replaced(agent_id):
            return None
    reps = [a for a in rows if a.resume_of == agent_id]
    rep = next((a for a in reps if a.ended_at is None), reps[-1] if reps else None)
    if me.left_reason == "paused":   # paused, then resumed as another session (maybe on another box)
        return (f"[swarm] Job \"{job}\" was paused and resumed: {rep.name if rep else 'a new session'} carries "
                f"on your work, from your stored transcript. Stop now: end your turn without further tool "
                f"calls, and do not report your task as done: say that the job was paused and resumed.")
    who = f"{rep.name} (restarted) has taken over your work" if rep else \
        "your work is left to a restart by the supervisor (if its budget allows)"
    why = f" ({me.left_reason})" if (me.left_reason or "").startswith(STUCK_PREFIX) else ""
    return (f"[swarm] The swarm supervisor of job \"{job}\" closed you as stuck{why}, and {who}. "
            f"Stop now: end your turn without further tool calls, and do not report your task as "
            f"done: say that the supervisor closed you as stuck.")


TASK_NOTICE_CHARS = 600   # how much of the new job's task a moved agent is shown


def _moved_notice(board, agent_id: str, name: str, job: str, old: str, roster, cfg: dict) -> str:
    """What an agent moved to `job` (swarm move / job merge) is told on its next turn, once: where it
    was moved from, the job's description, task and goal, the board instructions for the new job
    (its post command), the full roster with the judge, and a request to say hello. The catch-up of
    recent messages follows it (the move set its read cursor)."""
    js = board.job_status(job)
    about = []
    if js and js.description:
        about.append(f"- About: {_t(js.description)[:300]}")
    if js and js.task:
        about.append(f"- Task: {_t(js.task)[:TASK_NOTICE_CHARS]}")
    head = (f"[swarm] You were moved from job \"{_t(old)}\" to job \"{job}\" by the orchestrator, while you "
            f"kept running. From now on your board is job \"{job}\": post there, not on \"{_t(old)}\". "
            f"Read the job's recent messages below to learn what it is about, then post a short hello "
            f"saying what you are working on and your scope: `{_post_cmd(job, name)}`.")
    board.record_roster_sync(agent_id, roster_snapshot(roster, agent_id), True)
    return "\n".join([head, *about]) + "\n" + _instructions(
        name, job, cfg, js.goal if js else None, js.judge if js else None) + "\n\n" + roster_text(roster, agent_id, job)


def _on_turn(board, agent_id: str, name: str, job: str, cfg: dict, sid: str | None = None,
             payload: dict | None = None) -> None:
    from swarm.board.base import MOVED_PREFIX   # the board is open by now
    res = board.read_unread(agent_key=agent_id, job=job)
    roster, state = board.turn_state(agent_id, job)
    moved = state is not None and (state.roster_seen or "").startswith(MOVED_PREFIX)
    parts = []
    if moved:   # the job comes from the agent's row on every call: a move applies at once; tell it, once
        parts.append(_moved_notice(board, agent_id, name, job, state.roster_seen[len(MOVED_PREFIX):],
                                   roster, cfg))
        if payload is not None:
            _record_enrolment(cfg, agent_id, job, sid, payload)   # the local record follows the job
        state = dataclasses.replace(state, roster_seen=None, roster_synced_at=state.now)
    parts.append(_messages_text(res, job, name, "recent messages on this job (catch-up)" if moved
                                else "new messages"))
    if state is not None:
        parts.append(_reply_reminders(board, agent_id, job, name, state, {m.id for m in res.messages}))
        parts.append(_silence_nudge(board, agent_id, job, name, state, cfg))
        if not moved:
            parts.append(_roster_update(board, agent_id, job, roster, state, cfg))
        parts += _memory_turn(board, cfg, agent_id, job, state)
    text = "\n\n".join(p for p in parts if p)
    if text:
        _out("PreToolUse", text)


def _on_event(board, event: str, agent_id: str, sid: str | None, bound: dict, unbound: dict,
              payload: dict, cfg: dict, resume: dict | None = None) -> None:
    if event == "stop":
        host = current_host()
        try:        # before the stop: set_agent_runtime only touches active rows
            board.set_agent_runtime(agent_id, None, payload.get("model") or host.agent_model(payload, agent_id))
        except Exception as exc:   # never at the cost of the stop itself
            _log_error("stop model", agent_id, exc)
        if host.stop_is_final:
            board.agent_stopped(agent_id)
        else:   # Codex: this turn ended; the agent completes once no new turn comes (sweep_jobs)
            board.agent_turn_ended(agent_id)
        deadline = time.monotonic() + TRANSCRIPT_BUDGET_SECONDS
        _capture_stopped(board, cfg, agent_id, sid, payload, deadline)
        _sweep(board, cfg, event, agent_id, payload, deadline)
        return
    if event == "done":  # PostToolUse: the tool call finished; bookkeeping, and memory provenance
        board.tool_finished(agent_id)
        _memory_provenance(board, cfg, agent_id, sid, bound, payload)
        return
    if event == "start":
        stop = _paused_stop(board, agent_id) or _stuck_closed(board, agent_id)
        if stop:   # a stuck-closed (or paused) key is never enrolled again: it is only told to stop
            _out("SubagentStart", stop)
            return
        _route_new(board, event, agent_id, sid, bound, unbound, payload, cfg)
        # after the join: the agent's own job stays open
        _sweep(board, cfg, event, agent_id, payload, time.monotonic() + TRANSCRIPT_BUDGET_SECONDS)
        return
    # A key a restart row names never works again, even if its row was revived before this
    # check existed: checked before the active-member path. One indexed lookup, only for a
    # session with an active job.
    if bound and board.was_replaced(agent_id):
        stop = _stuck_closed(board, agent_id, replaced=True)
        if stop:
            _deny(stop)
            return
    member = board.tool_started(agent_id, payload.get("tool_name"))
    if member is None:  # not an active member (yet): route it, maybe enrol it
        if resume is not None:
            _enrol_resumed(board, agent_id, resume, payload, cfg)
            return
        stop = _paused_stop(board, agent_id) or _stuck_closed(board, agent_id)
        if stop:
            _deny(stop)
            return
        _route_new(board, event, agent_id, sid, bound, unbound, payload, cfg)
        _gate_new_member(board, agent_id, bound, payload, cfg)
        return
    if resume is not None and _first_call_of_resumed(board, agent_id, member.job):
        _enrol_resumed(board, agent_id, resume, payload, cfg)   # its seat was claimed by `swarm resume`
        return
    if resume is not None and current_host().is_spawn(payload):
        _deny_replacement_spawn(board, agent_id)
        return
    if member.job not in bound:  # its job is not (or no longer) one of this session's
        return
    if member.verify_tag:
        member = _verify_route(board, agent_id, sid, member, bound, unbound, payload, cfg)
        if member is None:
            _gate_new_member(board, agent_id, bound, payload, cfg)
            return
    if member.model is None:
        model = payload.get("model") or current_host().agent_model(payload, agent_id)
        if model:
            board.set_agent_runtime(agent_id, None, model)
    if _gate_verifier(board, agent_id, member.verifier, payload) and \
            _gate_spawn(board, agent_id, member.name, member.job, payload, cfg):
        _on_turn(board, agent_id, member.name, member.job, cfg, sid, payload)


def _memory_provenance(board, cfg: dict, agent_id: str, sid: str | None, bound: dict, payload: dict) -> None:
    """A shell call that saved a memory: pin it to this agent's transcript (swarm.provenance).
    Other tools cost nothing; other shell calls one precompiled regex (provenance.hint), which
    runs before anything else. Bounded by PROVENANCE_BUDGET_SECONDS. agent_id is this hook's
    own (payload, or a replacement's enrolment), never the call's output. Failures are logged,
    never raised (a kept claim is logged by provenance, with the document's owner)."""
    try:
        cmd = current_host().shell_command(payload)
        if not cmd:
            return
        from swarm import provenance
        if not provenance.hint(cmd, provenance.configured_writers(cfg)) or not provenance.enabled(cfg):
            return
        provenance.record_from_hook(board, cfg, current_host(), agent_id, sid, payload, cmd, bound,
                                    time.monotonic() + PROVENANCE_BUDGET_SECONDS)
    except Exception as exc:
        _log_error("done memory", agent_id, exc)


def _transcripts_on(cfg: dict) -> bool:
    return bool((cfg.get("transcripts") or {}).get("enabled"))


def _capture_stopped(board, cfg: dict, agent_id: str, sid: str | None, payload: dict,
                     deadline: float) -> None:
    """SubagentStop with [transcripts] on: store the agent's transcript, if it is (or was) on a
    job, from agent_transcript_path or else the path the host derives. Final, except after a
    Codex turn (the agent may get more). Failures (and running out of time) are logged, and a
    final one is recorded as a pending final (swarm.supervisor.lost.note_pending): the sweeps and
    the supervisor pass retry it, so an agent can't stop its own final archive by making its
    redaction outlast this hook's budget."""
    if not _transcripts_on(cfg):
        return
    try:
        from swarm import transcripts
        job = board.route(agent_id).member_job
        if not job:
            return
        state = board.sync_state(agent_id)
        host = current_host()
        path = payload.get("agent_transcript_path") or host.subagent_transcript(payload, agent_id)
        if not host.transcript_ok(path, sid, agent_id):   # a forged or stray path: read nothing
            _log_line("stop transcript", agent_id, f"skipped: {path} is not a transcript of this "
                                                   f"agent under the host's transcript root")
            return
        # a Codex turn is not final, unless the row has ended meanwhile (job closed in the quiet window)
        final = host.stop_is_final or board.active_agent_name(agent_id) is None
        began = time.monotonic()
        try:
            transcripts.capture_subagent(board, cfg, job, agent_id, path, final,
                                         state.name if state else None, sid, deadline,
                                         harness=current_host().name, use_mtime=False)
        except Exception as exc:
            if final:
                from swarm.supervisor import lost
                lost.note_pending(cfg, job, agent_id, current_host().name, exc, deadline - began)
            raise
    except Exception as exc:
        _log_error("stop transcript", agent_id, exc)


def _sweep(board, cfg: dict, event: str, agent_id: str, payload: dict | None = None,
           deadline: float | None = None) -> None:
    """Auto-close the jobs that are done and quiet (swarm.sweep_jobs), then, with [transcripts]
    on, a snapshot round if one is due. Only from SubagentStart and SubagentStop, once per
    agent: the per-tool-call hooks stay as cheap as they are. A failure here is logged and
    costs the agent nothing."""
    try:
        from swarm import cli as swarm
        swarm.sweep_jobs(board, cfg, deadline)
    except Exception as exc:
        _log_error(f"{event} sweep", agent_id, exc)
    if not _transcripts_on(cfg):
        return
    try:
        from swarm import transcripts
        payload = payload or {}
        hints = {payload.get("session_id"): payload.get("transcript_path")} \
            if payload.get("transcript_path") else None
        transcripts.snapshot(board, cfg, deadline, hints)
    except Exception as exc:
        _log_error(f"{event} transcript snapshot", agent_id, exc)


def _may_concern_session(event: str, bound: dict, unbound: dict) -> bool:
    """Whether this hook might have anything to do for its session, from the markers alone:
    a job bound to it, or an unbound one it could claim (on a start, or --adopt-running)."""
    if bound:
        return True
    if event == "start":
        return bool(unbound)
    return any(m.get("adopt_running") for m in unbound.values())


def _resume_turns(cfg: dict, sid: str, agent_id: str | None) -> None:
    """A follow-up tool call (Codex followup_task/send_input, from the root or a member) gives
    a child a new turn, and the child fires no hook until its next tool call. So the quiet
    windows of this session's jobs' ended agents restart now: coarse (which child it targets
    isn't mapped to a key), but safe: at worst another agent completes one window later. Never
    raises."""
    try:
        bound, _ = _session_markers(cfg, sid)
        if not bound:
            return
        from swarm.board import open_board
        with open_board(cfg, init_timeout=HOOK_INIT_TIMEOUT) as board:
            for job in bound:
                board.turns_resumed(job)
    except Exception as exc:
        _log_error("turn follow-up", agent_id or "-", exc)


def _orchestrator_seen(event: str, cfg: dict, sid: str | None, payload: dict) -> None:
    """A tool call of the orchestrating session itself: touch the "seen" file beside each marker
    bound to it (cli.mark_orchestrator_seen), so the auto-close sweep keeps its jobs open while
    it works, even when every agent has finished or is parked on a background wait. Files only,
    no board; all the touches together wait at most MARKER_SWEEP_WAIT for a marker lock (only a
    marker's first touch needs one). Failures are logged, never raised."""
    if not sid:
        return
    markers = _markers(cfg)
    _record_activations(cfg, sid, payload, markers)
    try:
        from swarm import cli
        deadline = time.monotonic() + cli.MARKER_SWEEP_WAIT
        for m in markers:
            if m.get("session_id") == sid:
                cli.mark_orchestrator_seen(m["_path"], deadline)
    except Exception as exc:
        _log_error(event, "main", exc)


def _orchestrator_respawn(event: str, cfg: dict, sid: str | None, payload: dict) -> None:
    """A not_met verdict with nobody left at work: tell the orchestrator (the only one who can
    spawn) to start the next round (swarm.respawn). Tool-call hooks add context; Stop refuses to
    end the turn, once. Only for jobs of this session that have a goal; never raises."""
    if not sid or event not in ("turn", "done", "session-stop"):
        return
    try:
        from swarm import respawn
        from swarm.board import open_board
        stop = event == "session-stop"
        if stop and payload.get("stop_hook_active"):
            return
        for m in _markers(cfg):
            if m.get("session_id") != sid or not m.get("goal") or "resume" in m:
                continue
            text = respawn.check(m["_path"], m["job"], force=stop, host=current_host(),
                                 open_board=lambda: open_board(cfg, init_timeout=HOOK_INIT_TIMEOUT))
            if not text:
                continue
            if stop:
                _OUTPUT.update({"decision": "block", "reason": text})
            else:
                _out("PreToolUse" if event == "turn" else "PostToolUse", text)
    except Exception as exc:
        _log_error(event, "main", exc)


def run_hook(event: str, cfg: dict, host: str | None = None) -> int:
    _OUTPUT.clear()
    _CURRENT["host"] = None
    try:
        _handle(event, cfg, host)
    finally:
        if _OUTPUT:
            print(json.dumps(_OUTPUT))
    return 0


def _handle(event: str, cfg: dict, host_flag: str | None = None) -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        return 0
    try:
        _CURRENT["host"] = hosts.get(hosts.detect_hook_host(host_flag, payload, os.environ))
    except KeyError:   # a host this version has no adapter for: do nothing
        return 0
    agent_id = payload.get("agent_id")
    sid = payload.get("session_id")
    if event == "turn" and sid and current_host().is_followup(payload):
        _resume_turns(cfg, sid, agent_id)
    resume = _resume_binding(cfg, sid) if not agent_id else None
    if resume is not None:   # a supervisor replacement's root session: it is the member
        agent_id = resume["resume"].get("agent_key") or sid
    elif not agent_id and _replacement_token():
        return 0   # a session started by (or inside) a replacement that isn't it: never an orchestrator
    if event == "session-stop" and (agent_id or resume is not None):
        return 0   # the end of a subagent's (or a replacement's) turn: SubagentStop is theirs
    if not agent_id:  # main session: note that it is at work; its spawns get models per role
        if event != "session-stop":
            _orchestrator_seen(event, cfg, sid, payload)
        _orchestrator_respawn(event, cfg, sid, payload)
        if event == "turn" and current_host().is_spawn(payload):
            try:
                _orchestrator_spawn(cfg, sid, payload)
            except Exception as exc:
                _log_error(event, "main", exc)
        return 0
    bound: dict = {}
    try:
        bound, unbound = _session_markers(cfg, sid) if sid else ({}, {})
        # A session with nothing to do here doesn't touch the database (SubagentStop always does:
        # the agent may be on a job whose marker is gone).
        if event != "stop" and not (sid and _may_concern_session(event, bound, unbound)):
            return 0
        # Imported only now: sessions without a swarm never load the board or its backend.
        _auto_init(event, agent_id, cfg)
        from swarm.board import open_board
        from swarm.spool import flush_spool
        with open_board(cfg, init_timeout=HOOK_INIT_TIMEOUT) as board:
            # Posts (and memories) spooled while the board was unreachable are delivered by
            # whichever hook next opens it, so they are not stranded until someone runs the CLI.
            # Bounded, so a backlog can't eat every hook's deadline: the rest waits for later hooks.
            try:
                per_tool = event in ("turn", "done")
                items, seconds, each = HOOK_FLUSH_TOOL if per_tool else HOOK_FLUSH_AGENT
                flush_spool(board, cfg, max_items=items, deadline=time.monotonic() + seconds,
                            op_timeout=each, memories=not per_tool)
            except Exception:
                pass
            _on_event(board, event, agent_id, sid, bound, unbound, payload, cfg, resume)
    except Exception as exc:  # never break the agent
        from swarm.board.base import JobPaused
        if isinstance(exc, JobPaused):   # a join into a paused job: tell the agent to stop, nothing else to do
            from swarm.pause import paused_stop_text
            text = paused_stop_text(exc.job, exc.paused_by, exc.reason)
            if event == "turn":
                _deny(text)
            elif event == "start":
                _out("SubagentStart", text)
            return 0
        _log_error(event, agent_id, exc)
        # ...except that the spawn caps fail closed: with a job of this session active, an Agent
        # call whose limits could not be checked is refused rather than let through.
        if event == "turn" and current_host().is_spawn(payload) and bound:
            _deny("[swarm] Spawn refused: the swarm board could not be reached to check the "
                  "spawn limits. Do the work yourself, or try again later.")
        # So do the verifier's restrictions: a write by an agent that may be a verifier is refused.
        elif event == "turn" and bound and _unchecked_verifier_write(payload, agent_id):
            _deny("[swarm] Refused: the swarm board could not be reached to check whether you are "
                  "a verifier (verifiers are read-only), and this call writes. Try again later.")
    return 0


def _unchecked_verifier_write(payload: dict, agent_id: str) -> bool:
    """For a tool call whose verifier gate could not run (board trouble): whether it writes (a
    writing tool, or a shell command that looks like it writes) and its agent may be a verifier
    (its spawn prompt says so, or can't be read). Never raises; an error here counts as yes."""
    try:
        host = current_host()
        if not host.tool_matches(payload.get("tool_name"), host.write_tools):
            from swarm.shellguard import writes_files
            cmd = host.shell_command(payload)
            if not (cmd and writes_files(cmd) is not None):
                return False
        prompt = _spawn_prompt(payload, agent_id)
        return prompt is None or _role(payload, agent_id, prompt) == "verifier"
    except Exception:
        return True
