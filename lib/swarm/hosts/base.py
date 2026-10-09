"""What differs between the agent hosts (Claude Code, Codex) behind one interface. The hook
logic (swarm.hooks) and the transcript archive only talk to a Host."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
import os
from swarm import compat

# Session and thread ids are UUIDs on both hosts. A board-supplied id is only ever compared
# with this (fullmatch) and then used as an exact file name: never as a glob or a path.
SESSION_ID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def valid_session_id(value) -> bool:
    return isinstance(value, str) and SESSION_ID.fullmatch(value) is not None


class ResumeUnsupported(Exception):
    """This agent can't be resumed from its stored transcript on this host (the message says why:
    a documented limit); the caller starts a briefed fresh agent instead."""


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
    supports_shell_rewrite = False      # a PreToolUse hook may rewrite a shell call's command
    reads_prompt_tags = True            # the swarm's [swarm ...] tags in spawn prompts are readable

    def completes_on(self, event: str) -> bool:
        """Whether this hook proves that an agent has finished."""
        return event == "stop" and self.stop_is_final

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

    # -- pause/resume (swarm.hosts.resume drives these) ---------------------------------------
    native_resume = False   # can start the agent from a stored transcript (else: digest only)

    def session_text(self, text: str, *, session_id: str, cwd: str) -> tuple[str, bool]:
        """The stored (redacted) transcript `text` rewritten as a top-level session of this host
        under `session_id` and `cwd`; (new text, whether a swarm-truncated gap was seen)."""
        raise NotImplementedError

    def write_session(self, text: str, *, session_id: str, cwd: str, root=None) -> Path:
        """Write a session_text result where this host's resume looks for it; the new file's path.
        Never overwrites: an existing file is an error."""
        raise NotImplementedError

    def resume_argv(self, cfg: dict, *, session_id: str, model: str | None) -> list[str]:
        """The non-interactive command that continues the written session; the prompt goes on stdin."""
        raise NotImplementedError

    def session_from_output(self, stdout: str) -> str | None:
        """The session id of a finished resume run, where the host reports one (else None)."""
        return None

    def cli_session_id(self, env: Mapping[str, str]) -> str | None:
        return None

    def rewrite_spawn_model(self, tool_input: dict, model: str) -> dict:
        """The complete replacement tool input (hosts replace, not merge) with `model` set."""
        return {**tool_input, "model": model}

    def shell_command(self, payload: dict) -> str | None:
        """The command line of a shell tool call (for the verifier's write check), else None."""
        return None

    def is_background_shell(self, payload: dict) -> bool:
        """Whether this shell call runs in the background (it returns at once and the command
        keeps running after the call, possibly after the agent): its command is then rewritten to
        run under `swarm bg` where supports_shell_rewrite. False where the host can't tell."""
        return False

    def input_rewrite_output(self, new_input: dict) -> dict:
        """The hookSpecificOutput fields that make this host run the call with `new_input`.
        Claude applies updatedInput without a permissionDecision (hooks.md, PreToolUse decision
        control), and adding "allow" would also skip its permission prompt, so it is left out."""
        return {"updatedInput": new_input}


def write_new_file(path: Path, text: str) -> Path:
    """Create `path` (parents too, 0700) with `text`, mode 0600: exclusive and not through a
    symlink, so a stored transcript never overwrites or follows anything already there."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = compat.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | compat.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text if text.endswith("\n") or not text else text + "\n")
    return path
