"""What differs between the agent hosts (Claude Code, Codex) behind one interface. The hook
logic (swarm.hooks) and the transcript archive only talk to a Host."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

# Session and thread ids are UUIDs on both hosts. A board-supplied id is only ever compared
# with this (fullmatch) and then used as an exact file name: never as a glob or a path.
SESSION_ID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def valid_session_id(value) -> bool:
    return isinstance(value, str) and SESSION_ID.fullmatch(value) is not None


@dataclass(frozen=True)
class SpawnCall:
    """What a spawn tool call asks for: the child's prompt, a short description for the board,
    and the model the caller picked (None: host default)."""
    prompt: str
    description: str
    model: str | None = None


class Host:
    name = ""
    spawn_tools: tuple[str, ...] = ()
    followup_tools: tuple[str, ...] = ()   # send an existing subagent a new turn (Codex)
    write_tools: tuple[str, ...] = ()   # refused for verifiers (with the spawn tools)
    stop_is_final = True                # SubagentStop means the agent is done (Codex: one turn is)
    supports_spawn_model_rewrite = False
    reads_prompt_tags = True            # the swarm's [swarm ...] tags in spawn prompts are readable

    @property
    def verifier_denied(self) -> tuple[str, ...]:
        return self.write_tools + self.spawn_tools

    def tool_matches(self, tool_name, names: tuple[str, ...]) -> bool:
        """Whether a hook's tool_name is one of `names` (Codex: namespaced names match by suffix)."""
        return tool_name in names

    def is_spawn(self, payload: dict) -> bool:
        return self.tool_matches(payload.get("tool_name"), self.spawn_tools)

    def is_followup(self, payload: dict) -> bool:
        return self.tool_matches(payload.get("tool_name"), self.followup_tools)

    def denies_verifier(self, payload: dict) -> bool:
        """Whether this tool call is one a verifier may not make (writing tools and spawns)."""
        return self.tool_matches(payload.get("tool_name"), self.verifier_denied)

    def spawn_call(self, tool_input: dict) -> SpawnCall:
        raise NotImplementedError

    def spawn_prompt(self, payload: dict, agent_id: str) -> str | None:
        """The subagent's spawn prompt; None while it can't be read yet, "" if readable but empty."""
        raise NotImplementedError

    def spawn_depth(self, payload: dict, agent_id: str) -> int | None:
        raise NotImplementedError

    def agent_model(self, payload: dict, agent_id: str) -> str | None:
        return None

    def role_hint(self, payload: dict, agent_id: str) -> str | None:
        """The agent's role where the host can't show its prompt tags; None if unspecified."""
        return None

    def spawn_role_hint(self, call: SpawnCall) -> str | None:
        """The role a spawn asks for where the prompt tags are unreadable; None."""
        return None

    def subagent_transcript(self, payload: dict, agent_id: str) -> Path | None:
        raise NotImplementedError

    def find_session_transcript(self, session_id: str | None, hint=None) -> Path | None:
        raise NotImplementedError

    def transcript_ok(self, path, session_id: str | None = None, agent_id: str | None = None) -> bool:
        """Whether `path` (from a hook payload, or derived from one) is a transcript this host
        writes for that session (agent_id None) or subagent: it resolves inside the host's
        transcript root and is named for the id. Nothing else is read into the archive. A host
        that can't tell says no."""
        return False

    def own_transcript(self, payload: dict) -> Path | None:
        """The transcript of the agent making this tool call (payload agent_id; without one, the
        session itself: a supervisor replacement's root), once transcript_ok accepts it,
        resolved. Never globs or searches (it runs in per-tool hooks). None if absent or refused."""
        return None

    def find_agent_transcript(self, main: Path | None, agent_id: str) -> Path | None:
        raise NotImplementedError

    def cli_session_id(self, env: Mapping[str, str]) -> str | None:
        return None

    def rewrite_spawn_model(self, tool_input: dict, model: str) -> dict:
        """The complete replacement tool input (hosts replace, not merge) with `model` set."""
        return {**tool_input, "model": model}

    def shell_command(self, payload: dict) -> str | None:
        """The command line of a shell tool call (for the verifier's write check), else None."""
        return None

    def input_rewrite_output(self, new_input: dict) -> dict:
        """The hookSpecificOutput fields that make this host run the call with `new_input`.
        Claude applies updatedInput without a permissionDecision (hooks.md, PreToolUse decision
        control), and adding "allow" would also skip its permission prompt, so it is left out."""
        return {"updatedInput": new_input}
