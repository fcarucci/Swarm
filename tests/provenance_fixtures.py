"""Hook payloads for memory-write tool calls on both hosts, built from real captures: Claude from
fixtures/claude/2.1.284/hooks/PostToolUse-Bash-subagent.json (a subagent's Bash call, captured with
a scratch --settings file, ids and text replaced), Codex from
fixtures/codex/0.157.1/hooks/PostToolUse-05.json (the child's Bash call). The builders replace
only the command, its output and the ids; every other key keeps the host's own shape.

Capture facts (Claude Code 2.1.284): `transcript_path` is the *session's* transcript even in a
subagent's payload (the subagent's own is <session>/subagents/agent-<agent_id>.jsonl beside it);
`tool_response` is a dict {stdout, stderr, interrupted, isImage, noOutputExpected}, and stdout
carries no trailing newline."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import codex_fixtures as CF

CLAUDE_FIX = Path(__file__).resolve().parent / "fixtures" / "claude" / "2.1.284" / "hooks" / "PostToolUse-Bash-subagent.json"

# A configured extra writer, as [provenance] writers spells it: `note-tool save` prints one
# `saved <doc> to <bank>` line per document.
NOTE_WRITERS = [{"name": "note-tool", "command": r"\bnote-tool\s+save\b",
                 "output": r"^saved (?P<doc>\S+) to (?P<bank>\S+)\s*$"}]
NOTE_CFG = {"provenance": {"writers": NOTE_WRITERS}}
NOTE_TOML = ("[provenance]\nwriters = [{ name = 'note-tool', command = '\\bnote-tool\\s+save\\b', "
             "output = '^saved (?P<doc>\\S+) to (?P<bank>\\S+)\\s*$' }]\n")


def add_note_writer(env) -> None:
    """Configure the `note-tool` writer in the Env's config file (kept above every other section, so
    a later `[hindsight]` rewrite keeps it) and reload env.cfg."""
    from swarm import cli as swarm
    env.config.write_text(NOTE_TOML + env.config.read_text())
    env.cfg = swarm.load_config(env.config)
NOTE_MULTI_CMD = "note-tool save --batch items.json"
NOTE_MULTI_OUT = "saved tool-batch-1 to notes\nsaved tool-batch-2 to notes\n"
NOTE_TOOL_CMD = "printf 'a fact' | note-tool save --doc-id tool-note-1"
NOTE_TOOL_OUT = "saved tool-note-1 to notes\n"
SWARM_REMEMBER_CMD = ("/home/alice/.claude/plugins/cache/swarm/swarm/0.1.0/bin/swarm remember "
                      "--job 'J' --as 'Homer Simpson' \"a fact\"")
SWARM_REMEMBER_OUT = 'remembered in project "J" [memory swarm-0123456789abcdef0123456789abcdef project "J"]\n'


def claude_post(command: str, stdout: str, *, agent_id: str = "a1", session_id: str = "sess-1",
                transcript_path: str | None = None, tool_use_id: str = "toolu_01MemoryWrite") -> dict:
    p = copy.deepcopy(json.loads(CLAUDE_FIX.read_text()))
    p.update(agent_id=agent_id, session_id=session_id, tool_use_id=tool_use_id, tool_name="Bash")
    if transcript_path is not None:
        p["transcript_path"] = transcript_path
    p["tool_input"] = {**p.get("tool_input", {}), "command": command}
    resp = p.get("tool_response")
    p["tool_response"] = {**resp, "stdout": stdout, "stderr": ""} if isinstance(resp, dict) else stdout
    return p


def codex_post(command: str, output: str, **override) -> dict:
    base = next(x for x in CF.payloads("PostToolUse") if x.get("agent_id") and x.get("tool_name") == "Bash")
    p = copy.deepcopy(base)
    p["tool_input"] = {**p.get("tool_input", {}), "command": command}
    p["tool_response"] = output
    p.update(override)
    return p


def output_of(payload: dict) -> str:
    r = payload.get("tool_response")
    return r.get("stdout", "") if isinstance(r, dict) else str(r)
