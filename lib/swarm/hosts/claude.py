"""Claude Code: spawn tool Agent (older name Task), prompt in the subagent's own transcript
<main minus .jsonl>/subagents/agent-<id>.jsonl, depth in agent-<id>.meta.json (spawnDepth)."""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import stat
import uuid
from pathlib import Path
from typing import Mapping

from .base import Host, SpawnCall, valid_session_id, write_new_file


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def projects_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude").expanduser() / "projects"


def subagent_path(main_transcript, agent_id: str) -> Path | None:
    main = str(main_transcript or "")
    if not main.endswith(".jsonl") or "/" in agent_id:
        return None
    return Path(main[:-len(".jsonl")]) / "subagents" / f"agent-{agent_id}.jsonl"


def project_dir_name(cwd: str) -> str:
    """How Claude Code names a working directory's folder under projects/: every character that
    is not a letter or digit becomes "-"."""
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _unanswered_tools(entries: list[dict]) -> list[str]:
    """tool_use ids in the last assistant entry that no later user entry answers (a pause that
    caught the agent mid tool call): Claude Code refuses to resume a conversation like that."""
    asked: dict[str, bool] = {}
    for e in entries:
        content = (e.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if not isinstance(b, dict):
                continue
            if e.get("type") == "assistant" and b.get("type") == "tool_use" and b.get("id"):
                asked[b["id"]] = False
            elif b.get("type") == "tool_result" and b.get("tool_use_id") in asked:
                asked[b["tool_use_id"]] = True
    return [i for i, answered in asked.items() if not answered]


class ClaudeHost(Host):
    name = "claude"
    native_resume = True
    spawn_tools = ("Agent", "Task")
    write_tools = ("Edit", "Write", "MultiEdit", "NotebookEdit")
    stop_is_final = True
    supports_spawn_model_rewrite = True

    def spawn_call(self, tool_input: dict) -> SpawnCall:
        ti = tool_input if isinstance(tool_input, dict) else {}
        prompt = ti.get("prompt") if isinstance(ti.get("prompt"), str) else ""
        what = ti.get("description") or ti.get("subagent_type") or "a subagent"
        model = ti.get("model") if isinstance(ti.get("model"), str) and ti.get("model") else None
        return SpawnCall(prompt, str(what), model)

    def _entries(self, payload: dict, agent_id: str, limit: int = 30):
        path = subagent_path(payload.get("transcript_path"), agent_id)
        if path is None:
            return None
        try:
            with open(path, encoding="utf-8") as fh:
                out = []
                for _ in range(limit):
                    line = fh.readline(4_000_000)
                    if not line:
                        break
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        break        # as the live _spawn_prompt: an undecodable line ends the scan
                return out
        except FileNotFoundError:
            return None
        except OSError:
            return []

    def spawn_prompt(self, payload: dict, agent_id: str) -> str | None:
        if isinstance(payload.get("prompt"), str):
            return payload["prompt"]
        entries = self._entries(payload, agent_id, 20)
        if entries is None:
            return None
        for e in entries:
            if isinstance(e, dict) and e.get("type") == "user":
                return _text_of((e.get("message") or {}).get("content"))
        return ""

    def spawn_depth(self, payload: dict, agent_id: str) -> int | None:
        path = subagent_path(payload.get("transcript_path"), agent_id)
        if path is None:
            return None
        try:
            depth = json.loads(path.with_suffix("").with_name(f"agent-{agent_id}.meta.json").read_text()).get("spawnDepth")
        except (OSError, ValueError, AttributeError):
            return None
        return depth if isinstance(depth, int) and not isinstance(depth, bool) else None

    def agent_model(self, payload: dict, agent_id: str) -> str | None:
        for e in self._entries(payload, agent_id) or []:
            if isinstance(e, dict) and e.get("type") == "assistant":
                model = (e.get("message") or {}).get("model")
                return model if isinstance(model, str) and model else None
        return None

    def subagent_transcript(self, payload: dict, agent_id: str) -> Path | None:
        return subagent_path(payload.get("transcript_path"), agent_id)

    def transcript_ok(self, path, session_id: str | None = None, agent_id: str | None = None) -> bool:
        """Exactly at, once resolved (symlinks and `..` included), <projects_dir>/<project>/
        <session_id>.jsonl, or <projects_dir>/<project>/<session_id>/subagents/agent-<agent_id>.jsonl."""
        if not path:
            return False
        try:
            real = Path(os.path.realpath(path))
            root = Path(os.path.realpath(projects_dir()))
        except (OSError, ValueError):
            return False
        if agent_id is None:   # <root>/<project>/<session>.jsonl
            return (real.parent.parent == root and real.suffix == ".jsonl"
                    and (not session_id or real.name == f"{session_id}.jsonl"))
        # <root>/<project>/<session>/subagents/agent-<id>.jsonl
        return (real.parents[3] == root if len(real.parents) > 3 else False) and \
            real.name == f"agent-{agent_id}.jsonl" and real.parent.name == "subagents" and \
            (not session_id or real.parent.parent.name == session_id)

    def own_transcript(self, payload: dict) -> Path | None:
        agent = payload.get("agent_id") if isinstance(payload.get("agent_id"), str) else None
        sid = payload.get("session_id")
        if not valid_session_id(sid):     # a session is a UUID; anything else reads nothing
            return None
        path = subagent_path(payload.get("transcript_path"), agent) if agent else payload.get("transcript_path")
        if not path or not self.transcript_ok(path, sid, agent):
            return None
        return Path(os.path.realpath(path))

    def find_session_transcript(self, session_id: str | None, hint=None) -> Path | None:
        if hint and self.transcript_ok(hint, session_id) and Path(hint).is_file():
            return Path(hint)
        if not valid_session_id(session_id):   # never a pattern or a path
            return None
        d = projects_dir()
        name = f"{session_id}.jsonl"
        try:
            projects = sorted(e.name for e in os.scandir(d) if e.is_dir(follow_symlinks=False))
        except OSError:
            return None
        for project in projects:   # <projects_dir>/<project>/<uuid>.jsonl, exact name, no links
            p = d / project / name
            try:
                # exact name, also on a case-insensitive filesystem (macOS): the entry must be there as is
                if stat.S_ISREG(os.lstat(p).st_mode) and name in os.listdir(d / project):
                    return p
            except OSError:
                continue
        return None

    def find_agent_transcript(self, main: Path | None, agent_id: str) -> Path | None:
        return subagent_path(main, agent_id) if main else None

    def cli_session_id(self, env: Mapping[str, str]) -> str | None:
        return env.get("CLAUDE_CODE_SESSION_ID") or None

    def shell_command(self, payload: dict) -> str | None:
        ti = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
        cmd = ti.get("command")
        return cmd if payload.get("tool_name") == "Bash" and isinstance(cmd, str) else None

    # -- pause/resume ----------------------------------------------------------------------
    # Claude Code resumes `--resume <uuid>` from <config>/projects/<cwd with non-alphanumerics as
    # "-">/<uuid>.jsonl, found by the current directory: a transcript stored from another box
    # works once it is written there under a session id and a cwd of this box. A stored subagent
    # transcript is a sidechain of its parent session (isSidechain, agentId): it is rewritten as
    # a session of its own. Not possible: the parent's subagent tree, file-history snapshots'
    # backups, and anything the redaction replaced (resumed context holds the placeholders).

    def session_text(self, text: str, *, session_id: str, cwd: str) -> tuple[str, bool]:
        entries: list[dict] = []
        truncated = False
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if not isinstance(e, dict):
                continue
            if e.get("type") == "swarm-truncated":
                truncated = True      # the stored copy was cut to head and tail: the gap is gone
                continue
            if "sessionId" in e:
                e["sessionId"] = session_id
            if "cwd" in e:
                e["cwd"] = cwd
            if e.get("isSidechain"):
                e["isSidechain"] = False
            e.pop("agentId", None)
            if isinstance(e.get("uuid"), str) and not e.get("timestamp"):
                e["timestamp"] = _now()   # Claude Code answers "No conversation found" without one
            entries.append(e)
        # a cut-off middle leaves the first kept entries pointing at parents that are gone
        known = {e["uuid"] for e in entries if isinstance(e.get("uuid"), str)}
        if truncated:
            for e in entries:
                if e.get("parentUuid") not in known and e.get("parentUuid") is not None:
                    e["parentUuid"] = None
        pending = _unanswered_tools(entries)
        if pending:
            last = next((e for e in reversed(entries) if isinstance(e.get("uuid"), str)), {})
            now = _now()
            parent = last.get("uuid")
            blocks = [{"type": "tool_result", "tool_use_id": i, "is_error": True,
                       "content": "Interrupted: the job was paused before this tool call finished."}
                      for i in pending]
            uid = str(uuid.uuid4())
            entries.append({"type": "user", "uuid": uid, "parentUuid": parent, "sessionId": session_id,
                            "cwd": cwd, "isSidechain": False, "userType": "external", "timestamp": now,
                            "message": {"role": "user", "content": blocks}})
        return "\n".join(json.dumps(e, ensure_ascii=False, separators=(",", ":")) for e in entries), truncated

    def write_session(self, text: str, *, session_id: str, cwd: str, root=None) -> Path:
        if not valid_session_id(session_id):
            raise ValueError("session id must be a UUID")
        if not os.path.isabs(cwd):
            raise ValueError("cwd must be an absolute path")
        base = Path(root) / "projects" if root else projects_dir()
        return write_new_file(base / project_dir_name(cwd) / f"{session_id}.jsonl", text)

    def resume_argv(self, cfg: dict, *, session_id: str, model: str | None) -> list[str]:
        from swarm.supervisor.launch import check_safe
        from swarm.supervisor.settings import settings
        s = settings(cfg)
        argv = [s["claude_bin"], "-p", "--resume", session_id, "--max-turns", str(s["max_turns"]),
                "--permission-mode", s["claude_permission_mode"], "--output-format", "json",
                "--setting-sources", "user"]
        if model:
            argv += ["--model", model]
        if s.get("claude_plugin_dir"):
            argv += ["--plugin-dir", s["claude_plugin_dir"]]
        check_safe(argv)
        return argv

    def session_from_output(self, stdout: str) -> str | None:
        try:
            sid = json.loads(stdout).get("session_id")
        except (ValueError, AttributeError):
            return None
        return sid if valid_session_id(sid) else None
