"""Codex CLI (0.157.1, multi-agent V2): spawn tool spawn_agent, reported to hooks with its
namespace glued on ("collaborationspawn_agent"), so tool names match by suffix. Its arguments are
task_name (plaintext), fork_turns and message, which V2 encrypts ("gAAAAA..."): the child gets it
as an agent_message whose content is a plaintext header plus encrypted_content, so the swarm's
prompt tags can't be read on Codex V2 (routing uses the session's bound job, the role the task
name). Each thread has its own rollout
$CODEX_HOME/sessions/Y/M/D/rollout-<ts>-<thread>.jsonl whose first line is its session_meta
(payload.source carries depth and agent_path; a forked child's rollout has the parent's
session_meta after its own). The hooks' session_id is the root thread id, agent_id the
subagent's thread id. Write tool apply_patch; the shell is reported as "Bash". SubagentStop fires
after every child turn. Facts: tests/fixtures/codex/0.157.1."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Mapping

from .base import SESSION_ID, Host, SpawnCall

# Where session_meta.payload.source keeps the depth (see ANSWERS.md in the fixtures), and the path.
CODEX_DEPTH_PATH = ("subagent", "thread_spawn", "depth")
CODEX_AGENT_PATH = ("subagent", "thread_spawn", "agent_path")
# User-role items Codex writes itself before the task (environment, AGENTS.md, ...).
CONTEXT_PREFIXES = ("<environment_context>", "<user_instructions>", "<permissions", "# AGENTS.md", "<INSTRUCTIONS>")
# The plaintext header of an inter-agent message; the payload follows "Payload:\n".
TASK_HEADER = "Message Type: NEW_TASK\n"
SHELL_TOOLS = ("Bash", "shell", "exec_command", "local_shell", "unified_exec")
SCAN_LINES = 200
# Thread ids are UUIDs; anything else (glob metacharacters from a CLI argument, a path) finds nothing.
THREAD_ID = SESSION_ID


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or "~/.codex").expanduser()


def find_rollout(thread_id: str) -> Path | None:
    if not isinstance(thread_id, str) or not THREAD_ID.fullmatch(thread_id):
        return None
    home = codex_home()
    for pattern in (f"sessions/*/*/*/rollout-*-{thread_id}.jsonl", f"sessions/*/*/*/rollout-*-{thread_id}.jsonl.zst",
                    f"archived_sessions/**/rollout-*-{thread_id}.jsonl",
                    f"archived_sessions/**/rollout-*-{thread_id}.jsonl.zst"):
        hit = next(iter(sorted(home.glob(pattern))), None)
        if hit:
            return hit
    return None


class RolloutUnreadable(Exception):
    """A rollout that exists but can't be read or decompressed (the message says why)."""


class RolloutTooLarge(RolloutUnreadable):
    """A rollout over the read limit (transcripts.RAW_READ_MAX), on disk or once decompressed: it
    is never read whole (a few KB of zstd can expand to gigabytes). A final capture
    marks it capture failed, too large."""

    def __init__(self, msg: str, size: int, limit: int):
        super().__init__(msg)
        self.size, self.limit = size, limit


ZSTD_TOOL_SECONDS = 30.0   # the zstd tool fallback runs at most this long (or to the deadline)
_CHUNK = 1 << 20


def _too_large(p: Path, size: int, limit: int, what: str) -> RolloutTooLarge:
    return RolloutTooLarge(f"{p}: {what} over the {limit} byte read limit", size, limit)


def _zstd_tool(p: Path, limit: int, deadline: float | None) -> bytes:
    """`zstd -dc` with its output read up to limit + 1 bytes, within ZSTD_TOOL_SECONDS or the
    deadline (whichever is sooner); killed past either."""
    import select
    import time
    stop = time.monotonic() + ZSTD_TOOL_SECONDS
    if deadline is not None:
        stop = min(stop, deadline)
    try:
        proc = subprocess.Popen(["zstd", "-dc", str(p)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                stdin=subprocess.DEVNULL)
    except OSError as exc:
        raise RolloutUnreadable(f"{p}: zstd failed ({exc})") from exc
    out, n, timed_out = [], 0, False
    try:
        fd = proc.stdout.fileno()
        while True:
            left = stop - time.monotonic()
            if left <= 0:
                timed_out = True
                break
            ready, _, _ = select.select([fd], [], [], left)
            if not ready:
                continue
            chunk = os.read(fd, _CHUNK)
            if not chunk:
                break
            out.append(chunk)
            n += len(chunk)
            if n > limit:
                raise _too_large(p, n, limit, "decompressed (zstd tool)")
    finally:
        proc.kill()
        proc.wait()
        proc.stdout.close()
    if timed_out:
        if deadline is not None and time.monotonic() >= deadline:
            from swarm.transcripts import OutOfTime
            raise OutOfTime("transcript capture ran out of time")
        raise RolloutUnreadable(f"{p}: zstd took over {ZSTD_TOOL_SECONDS:g} s")
    if proc.returncode not in (0, -9):
        raise RolloutUnreadable(f"{p}: zstd exited {proc.returncode}")
    return b"".join(out)


def read_rollout(path: Path, limit: int | None = None, deadline: float | None = None) -> str:
    """The rollout's text. `.jsonl.zst` is decompressed with the zstandard package (in the
    plugin's venv, requirements.txt), else the zstd tool. Raises RolloutUnreadable, with the
    reason, instead of ever returning text it couldn't read or that isn't a rollout: invalid
    UTF-8, no lines, a first line that isn't session_meta, or a complete line that isn't a JSON
    object. A last line without its newline counts if it is a complete JSON object, and is left
    out (Codex is still writing it) if not. Never reads more than `limit` bytes (default
    transcripts.RAW_READ_MAX), on disk or decompressed: past it, RolloutTooLarge; `deadline` (a
    time.monotonic() value) bounds the decompression (transcripts.OutOfTime)."""
    if limit is None:
        from swarm.transcripts import RAW_READ_MAX
        limit = RAW_READ_MAX
    p = Path(path)
    try:
        size = p.stat().st_size
        if size > limit:
            raise _too_large(p, size, limit, f"{size} bytes on disk")
        data = p.read_bytes()
    except OSError as exc:
        raise RolloutUnreadable(f"{p}: {exc.strerror or exc}") from exc
    if len(data) > limit:
        raise _too_large(p, len(data), limit, f"{len(data)} bytes on disk")
    if p.name.endswith(".zst"):
        try:
            import zstandard
        except ImportError:
            zstandard = None
        if zstandard is not None:
            try:
                import io
                import time
                parts, n = [], 0
                with zstandard.ZstdDecompressor().stream_reader(io.BytesIO(data), read_across_frames=True) as r:
                    while True:   # in chunks: never more than limit + 1 bytes, the deadline between them
                        if deadline is not None and time.monotonic() > deadline:
                            from swarm.transcripts import OutOfTime
                            raise OutOfTime("transcript capture ran out of time")
                        chunk = r.read(min(_CHUNK, limit + 1 - n))
                        if not chunk:
                            break
                        parts.append(chunk)
                        n += len(chunk)
                        if n > limit:
                            raise _too_large(p, n, limit, "decompressed")
                data = b"".join(parts)
            except zstandard.ZstdError as exc:
                raise RolloutUnreadable(f"{p}: not valid zstd ({exc})") from exc
        elif shutil.which("zstd"):
            data = _zstd_tool(p, limit, deadline)
        else:
            raise RolloutUnreadable(f"{p}: compressed rollout, but neither the zstandard package nor the zstd "
                                    f"tool is available (the plugin venv should have zstandard: run "
                                    f"`~/.local/bin/swarm doctor`)")
    return _checked(p, data)


def _checked(p: Path, data: bytes) -> str:
    """The validated text. Everything up to the last newline must be complete, valid lines. The
    part after it (Codex may be writing it right now) is kept only if it is a complete JSON
    object already; a partial record or a multibyte character cut short is left out, not an error."""
    cut = data.rfind(b"\n") + 1
    body, tail = data[:cut], data[cut:]
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RolloutUnreadable(f"{p}: not UTF-8 (byte {exc.start})") from exc
    lines = text.splitlines()
    if tail.strip():
        try:
            last = tail.decode("utf-8")
            if isinstance(json.loads(last), dict):
                text, lines = text + last, lines + [last]
        except ValueError:            # UnicodeDecodeError is a ValueError too
            pass
    if not any(line.strip() for line in lines):
        raise RolloutUnreadable(f"{p}: no complete lines")
    first = True
    for n, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except ValueError as exc:
            raise RolloutUnreadable(f"{p}: line {n} is not JSON ({exc.msg})") from exc
        if not isinstance(e, dict):
            raise RolloutUnreadable(f"{p}: line {n} is not a JSON object")
        if first and e.get("type") != "session_meta":
            raise RolloutUnreadable(f"{p}: line {n} is not the session_meta header")
        first = False
    return text


def _entries(path: Path | None, limit: int = SCAN_LINES) -> list[dict] | None:
    if path is None or not Path(path).exists():
        return None
    try:
        text = read_rollout(Path(path))
    except RolloutUnreadable as exc:
        _log(str(exc))
        return None       # can't tell yet: the caller treats it as "not readable"
    out = []
    for line in text.splitlines()[:limit]:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict):
            out.append(e)
    return out


def _log(message: str) -> None:
    """Into the hooks' error log; never raises."""
    try:
        from swarm.hooks import _log_line
        _log_line("codex", "-", message)
    except Exception:
        pass


def _task_texts(entries: list[dict], me: str | None) -> list[str] | None:
    """The plaintext of the agent's task: user messages that aren't Codex's own context, and the
    plaintext payload of NEW_TASK agent_messages sent to `me` (its agent path, when known); an
    encrypted payload adds "". A forked child's rollout (spawn_agent fork_turns other than
    "none"; "all" is the default) starts with the parent's history: records before its own
    session_meta's subagent_history_start_ordinal are inherited and don't count. None while
    nothing has arrived."""
    texts, seen = [], False
    start = _own_history_start(entries)
    for n, e in enumerate(entries):
        p = e.get("payload") or {}
        if e.get("type") != "response_item":
            continue
        ordinal = e.get("ordinal") if isinstance(e.get("ordinal"), int) else n
        if ordinal < start:
            continue       # the parent's history, copied in by the fork
        parts = [c.get("text", "") for c in p.get("content") or []
                 if isinstance(c, dict) and c.get("type") in ("input_text", "text") and isinstance(c.get("text"), str)]
        if p.get("type") == "message" and p.get("role") == "user":
            t = "\n".join(parts)
            if t.strip() and not t.lstrip().startswith(CONTEXT_PREFIXES):
                seen = True
                texts.append(t)
        elif p.get("type") == "agent_message" and parts and parts[0].startswith(TASK_HEADER) \
                and (me is None or p.get("recipient") in (None, me)):
            seen = True
            _, _, payload = parts[0].partition("Payload:\n")
            t = "\n".join([payload, *parts[1:]]).strip("\n")
            if t.strip():
                texts.append(t)
    return texts if seen else None


def _own_history_start(entries: list[dict]) -> int:
    """The first rollout ordinal of the agent's own history: 0, unless its own session_meta (the
    first line) says it was forked. Codex tag rust-v0.157.1 (commit 36650394),
    codex-rs/protocol/src/protocol.rs, struct SessionMeta: `forked_from_id` (line 3127 at that
    commit) and `subagent_history_start_ordinal` (line 3185: "First rollout ordinal
    that belongs to this subagent's own projected history. Earlier rollout records are inherited
    model context"); every rollout line carries its "ordinal" (as captured in the fixtures).
    A forked rollout also repeats the parent's session_meta after its own
    (core/src/agent/control/spawn.rs keeps SessionMeta items when copying the parent's history),
    so the number of session_meta lines is not the test: a fork is only what the child's own
    header says."""
    meta = entries[0].get("payload") if entries and entries[0].get("type") == "session_meta" else None
    if not isinstance(meta, dict) or not meta.get("forked_from_id"):
        return 0
    start = meta.get("subagent_history_start_ordinal")
    return start if isinstance(start, int) and not isinstance(start, bool) else 0


def _meta_source(payload: dict, agent_id: str):
    """session_meta.payload.source of the agent's own rollout (its first line), else None."""
    entries = _entries(_own_rollout(payload, agent_id), 1)
    if not entries or entries[0].get("type") != "session_meta":
        return None
    return (entries[0].get("payload") or {}).get("source")


def _dig(node, path: tuple[str, ...]):
    for key in path:
        node = node.get(key) if isinstance(node, dict) else None
    return node


def _role_of_name(task_name: str) -> str | None:
    from swarm.roles import from_task_name
    return from_task_name(task_name)


def _own_rollout(payload: dict, agent_id: str) -> Path | None:
    tp = payload.get("transcript_path")
    if isinstance(tp, str) and tp and Path(tp).exists():
        return Path(tp)
    return find_rollout(agent_id)


class CodexHost(Host):
    name = "codex"
    spawn_tools = ("spawn_agent",)
    followup_tools = ("followup_task", "send_input")   # V2, V1
    write_tools = ("apply_patch",)
    stop_is_final = False
    # ANSWERS.md Q4: updatedInput is honoured with permissionDecision "allow"
    # (codex-rs/hooks/src/engine/output_parser.rs:162; applied at core/src/tools/registry.rs:613), and
    # spawn_agent declares `model: Option<String>` (core/src/tools/handlers/multi_agents_v2/spawn.rs:259).
    supports_spawn_model_rewrite = True
    reads_prompt_tags = False     # V2 encrypts the spawn message: roles come from the task name

    def tool_matches(self, tool_name, names: tuple[str, ...]) -> bool:
        """Codex prefixes a tool's namespace with no separator ("collaborationspawn_agent")."""
        return isinstance(tool_name, str) and any(tool_name == n or tool_name.endswith(n) for n in names)

    def spawn_call(self, tool_input: dict) -> SpawnCall:
        ti = tool_input if isinstance(tool_input, dict) else {}
        prompt = ti.get("message") if isinstance(ti.get("message"), str) else "\n".join(
            str(i.get("text", "")) for i in ti.get("items") or [] if isinstance(i, dict))
        model = ti.get("model") if isinstance(ti.get("model"), str) and ti.get("model") else None
        what = ti.get("task_name") or ti.get("agent_type") or "a subagent"
        return SpawnCall(prompt, str(what), model)

    def spawn_prompt(self, payload: dict, agent_id: str) -> str | None:
        """None until the task message is in the agent's rollout (not yet at SubagentStart,
        ANSWERS.md Q1); then its plaintext, which is "" for V2's encrypted message."""
        entries = _entries(_own_rollout(payload, agent_id))
        if entries is None:
            return None
        meta = entries[0] if entries and entries[0].get("type") == "session_meta" else {}
        me = _dig((meta.get("payload") or {}).get("source"), CODEX_AGENT_PATH)
        texts = _task_texts(entries, me if isinstance(me, str) else None)
        return None if texts is None else "\n".join(texts)

    def spawn_depth(self, payload: dict, agent_id: str) -> int | None:
        source = _meta_source(payload, agent_id)
        if isinstance(source, str):
            return 0            # a root thread (exec, cli, ...)
        node = _dig(source, CODEX_DEPTH_PATH)
        return node if isinstance(node, int) and not isinstance(node, bool) else None

    def agent_path(self, payload: dict, agent_id: str) -> str | None:
        """The agent's path in the team ("/root/fixture"); its last part is the spawn's task_name."""
        node = _dig(_meta_source(payload, agent_id), CODEX_AGENT_PATH)
        return node if isinstance(node, str) and node else None

    def role_hint(self, payload: dict, agent_id: str) -> str | None:
        """From the agent's task name, the last part of its agent path ("/root/verifier-2")."""
        path = self.agent_path(payload, agent_id)
        return _role_of_name(path.rsplit("/", 1)[-1]) if path else None

    def spawn_role_hint(self, call: SpawnCall) -> str | None:
        return _role_of_name(call.description)

    def agent_model(self, payload: dict, agent_id: str) -> str | None:
        m = payload.get("model")
        return m if isinstance(m, str) and m else None

    def subagent_transcript(self, payload: dict, agent_id: str) -> Path | None:
        p = payload.get("agent_transcript_path")
        return Path(p) if isinstance(p, str) and p else find_rollout(agent_id)

    def transcript_ok(self, path, session_id: str | None = None, agent_id: str | None = None) -> bool:
        """A rollout of that thread (agent_id, else session_id), once resolved exactly where Codex
        keeps them: <codex_home>/sessions/Y/M/D/ or anywhere under <codex_home>/archived_sessions/,
        named rollout-*-<thread>.jsonl[.zst]."""
        thread = agent_id or session_id
        if not path or not isinstance(thread, str) or not THREAD_ID.fullmatch(thread):
            return False
        try:
            real = Path(os.path.realpath(path))
            home = Path(os.path.realpath(codex_home()))
        except (OSError, ValueError):
            return False
        if not (real.name.startswith("rollout-") and
                real.name.endswith((f"-{thread}.jsonl", f"-{thread}.jsonl.zst"))):
            return False
        return (len(real.parents) > 4 and real.parents[3] == home / "sessions") or \
            (home / "archived_sessions") in real.parents

    def own_transcript(self, payload: dict) -> Path | None:
        """The payload's own rollout path (a child's hooks carry the child's; ANSWERS.md), checked;
        never find_rollout (a glob) from a per-tool hook."""
        agent = payload.get("agent_id") if isinstance(payload.get("agent_id"), str) else None
        sid = payload.get("session_id") if isinstance(payload.get("session_id"), str) else None
        path = payload.get("agent_transcript_path") or payload.get("transcript_path")
        if not isinstance(path, str) or not path or not self.transcript_ok(path, sid, agent):
            return None
        return Path(os.path.realpath(path))

    def find_session_transcript(self, session_id: str | None, hint=None) -> Path | None:
        if hint and session_id and self.transcript_ok(hint, session_id) and Path(hint).is_file() \
                and str(hint).endswith(f"-{session_id}.jsonl"):
            return Path(hint)
        return find_rollout(session_id or "")

    def find_agent_transcript(self, main: Path | None, agent_id: str) -> Path | None:
        return find_rollout(agent_id)

    def cli_session_id(self, env: Mapping[str, str]) -> str | None:
        return env.get("CODEX_SESSION_ID") or None     # ANSWERS.md Q3: equals the hooks' session_id

    def shell_command(self, payload: dict) -> str | None:
        """Codex reports its shell as tool_name "Bash" in hooks, the command a string (fixture);
        an argv list is joined."""
        if not self.tool_matches(payload.get("tool_name"), SHELL_TOOLS):
            return None
        ti = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
        cmd = ti.get("command", ti.get("cmd"))
        if isinstance(cmd, list):
            return " ".join(str(c) for c in cmd)
        return cmd if isinstance(cmd, str) else None

    def input_rewrite_output(self, new_input: dict) -> dict:
        """Codex honours updatedInput only together with permissionDecision "allow"; any other
        shape fails the hook run and the call proceeds unchanged (Codex hooks docs, PreToolUse).
        For a local function tool such as spawn_agent it is the complete arguments object."""
        return {"permissionDecision": "allow", "updatedInput": new_input}
