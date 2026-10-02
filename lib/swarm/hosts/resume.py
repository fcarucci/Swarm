"""Re-create a paused agent on this box from the board alone.

restore(board, manifest, ...) reads the agent's stored transcript with Board.transcript_body (the
redacted text in the database, never a file on this or any disk), and, depending on the mode:

* "resume": rewrites it as a session of this box's host (Host.session_text / write_session) and
  returns the command that resumes it (Claude Code `--resume <uuid>`; Codex `exec resume`);
* "briefing": no session is written; the prompt carries a recap of the last turns (the same
  rendering as `swarm transcript show`) and the agent starts fresh. Used when the harness
  differs from the manifest's (a Claude agent resumed under Codex or the other way round), when
  the stored copy is unusable or cut to head and tail beyond repair, or for Codex unless
  native="codex" is passed (writing a rollout is not verified against a live Codex).

Either way the prompt (stdin) is the "paused and resumed on host X" note: who the agent is, its
read cursor on the board, its last tool and task. Nothing here starts a process: start() does,
with a LaunchSpec-like result checked by supervisor.launch.check_safe. The manifest is board data
and untrusted: names and text are term_safe'd and redacted again, ids are validated, the working
directory must already exist on this box.
"""
from __future__ import annotations

import datetime as _dt
import os
import socket
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import get as get_host
from .base import valid_session_id

DIGEST_TURNS = 40
DIGEST_CHARS = 24000
FIELD_MAX = 400


from .base import ResumeUnsupported  # noqa: E402,F401  (re-exported: the documented limit)


class ResumeError(ResumeUnsupported):
    """The agent can't be restored (no stored transcript, unknown host, bad working directory):
    core falls back to a plain briefing."""


@dataclass(frozen=True)
class Restored:
    harness: str                  # the host that will run it
    mode: str                     # "resume" | "briefing"
    argv: tuple[str, ...]         # to run, prompt on stdin
    cwd: str
    stdin: str                    # the resume note (plus the recap in digest mode)
    session_id: str | None        # the new session written (native), else None
    path: Path | None             # the file written (native), else None
    truncated: bool = False       # the stored copy had a gap
    notes: tuple[str, ...] = field(default_factory=tuple)   # why this mode, limitations hit


def _clean(value, n: int = FIELD_MAX) -> str:
    from swarm.textsafe import term_safe
    from swarm.transcripts import redact
    text = " ".join(term_safe(value if value is not None else "").split())
    return redact(text)[0][:n]


def resume_note(manifest: dict, host_label: str, now: _dt.datetime | None = None) -> str:
    """The 'you were paused and resumed on host X' message, from the manifest only."""
    now = now or _dt.datetime.now(_dt.timezone.utc)
    m = manifest
    lines = [f"[swarm] You were paused on job \"{_clean(m.get('job'))}\""
             + (f" ({_clean(m.get('reason'))})" if m.get("reason") else "")
             + f" and resumed on host {_clean(host_label, 120)} at {now.strftime('%Y-%m-%d %H:%M UTC')}."
             f" You are still {_clean(m.get('agent_name'))}"
             + (f", role {_clean(m.get('role'))}" if m.get("role") else "") + "."]
    if m.get("cursor") is not None:
        lines.append(f"Your board read cursor was message #{_clean(m.get('cursor'), 40)}: read what was "
                     f"posted since with `swarm who` and the board feed before you continue.")
    if m.get("last_tool"):
        lines.append(f"Your last tool call before the pause was {_clean(m.get('last_tool'), 120)}; "
                     f"check whether it finished (files, processes) before repeating it.")
    if m.get("task"):
        lines.append(f"Your task: {_clean(m.get('task'), 1000)}")
    lines.append("This machine is not the one you ran on: paths, processes, and shell state from before "
                 "the pause may be gone. Re-check them, then continue the work.")
    return "\n".join(lines)


def digest(text: str, turns: int = DIGEST_TURNS, chars: int = DIGEST_CHARS) -> str:
    """A recap of the last `turns` turns of a stored transcript (either host's format)."""
    from swarm import transcript_view as tv
    out = tv.render_text(tv.select(tv.turns(text), tail=turns))
    return out if len(out) <= chars else "...\n" + out[-chars:]


def _workdir(manifest: dict, workdir: str | None) -> str:
    for cand in (workdir, manifest.get("cwd")):
        if isinstance(cand, str) and os.path.isabs(cand) and os.path.isdir(cand):
            return os.path.realpath(cand)
    if workdir:
        raise ResumeError(f"working directory {_clean(workdir, 200)} does not exist on this box")
    return os.path.realpath(os.getcwd())


def fetch_text(board, job: str, agent_key: str) -> str:
    """The stored transcript (redacted JSONL, images restored) from the board."""
    from swarm import transcripts
    body = board.transcript_body(job, agent_key)
    if not body:
        raise ResumeError(f"no stored transcript for {_clean(agent_key, 80)} on job {_clean(job, 80)}")
    text = body.decode("utf-8", errors="replace")
    if transcripts.IMAGE_TYPE in text:
        text = transcripts.restore_images(text, lambda sha: getattr(board.transcript_image(sha), "data", None))
    return text


def restore(board, manifest: dict, host: str | None = None, dest_root=None,
            host_label: str | None = None, *, cfg: dict | None = None, native: str | None = None,
            workdir: str | None = None, now: _dt.datetime | None = None, write: bool = True) -> Restored:
    """Prepare one agent (a flat manifest entry) to run on this box. `host`: the adapter to run
    it on (default: the entry's harness). `dest_root`: the host's store root to write the session
    under (default: the host's own, CLAUDE_CONFIG_DIR / CODEX_HOME). `native="codex"` lets Codex
    use its experimental rollout write. `write=False`: build the command without writing."""
    cfg = cfg or {}
    harness = host
    job, key = manifest.get("job"), manifest.get("agent_key")
    if not isinstance(job, str) or not isinstance(key, str):
        raise ResumeError("manifest needs job and agent_key")
    src = manifest.get("harness") or "claude"
    target = harness or src
    try:
        host = get_host(target)
    except KeyError:
        raise ResumeError(f"unknown host {_clean(target, 40)}") from None
    text = fetch_text(board, job, key)
    cwd = _workdir(manifest, workdir)
    label = host_label or socket.gethostname()
    note = manifest["note"] if isinstance(manifest.get("note"), str) and manifest["note"] else resume_note(manifest, label, now)
    notes: list[str] = []
    model = manifest.get("model") if isinstance(manifest.get("model"), str) else None
    from swarm.supervisor.launch import replacement_model
    model = replacement_model(cfg, target, manifest.get("role"), model)
    mode, why = "resume", None
    if target != src:
        mode, why = "briefing", f"manifest is from {src}, running on {target}: a transcript can't cross harnesses"
    elif target == "codex" and native != "codex":
        mode, why = "briefing", "Codex native rollout resume is experimental and not enabled (native='codex')"
    if mode == "resume":
        sid = str(uuid.uuid4())
        try:
            session, truncated = host.session_text(text, session_id=sid, cwd=cwd)
        except ValueError as exc:
            mode, why, truncated = "briefing", f"stored transcript not usable as a session: {exc}", False
        else:
            if not session.strip():
                mode, why = "briefing", "stored transcript has no entries"
    if mode == "resume":
        if truncated:
            notes.append("stored copy was cut to head and tail: the middle of the conversation is missing")
            note += "\nPart of your earlier conversation was too large to keep and is missing."
        path = host.write_session(session, session_id=sid, cwd=cwd, root=dest_root) if write else None
        argv = host.resume_argv(cfg, session_id=sid, model=model)
        return Restored(target, "resume", tuple(argv), cwd, note, sid, path, truncated, tuple(notes))
    notes.append(why or "briefing mode")
    recap = digest(text)
    prompt = f"{note}\n\nThe end of your earlier conversation (a recap, older turns omitted):\n\n{recap}"
    sid = str(uuid.uuid4()) if target == "claude" else None
    from swarm.supervisor import launch
    spec = launch.spec_for(cfg, target, prompt=prompt, workdir=cwd, model=model,
                           minutes=float(manifest.get("minutes") or 30), **({"session_id": sid} if sid else {}))
    return Restored(target, "briefing", spec.argv, cwd, prompt, spec.session_id, None, False, tuple(notes))


def start(restored: Restored, env: dict | None = None, popen=subprocess.Popen):
    """Run a Restored in the background in its working directory, prompt on stdin, output
    discarded (the agent talks through the board). Returns the process."""
    from swarm.supervisor.launch import check_safe
    check_safe(restored.argv)
    proc = popen(list(restored.argv), cwd=restored.cwd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                 stderr=subprocess.DEVNULL, env=env, start_new_session=True, text=True)
    proc.stdin.write(restored.stdin)
    proc.stdin.close()
    return proc
