"""The typed request protocol between a jailed drive seat and its runner's broker (Task 20, round 2).

A seat never hands the broker an argv. Its `swarm` command (seatbroker_client, inside the jail) parses the
command line itself against SCHEMA and sends {"v": 2, "kind": "cli", "command": NAME, "fields": {...}}:
only the commands in SCHEMA, only their fields, no global option (no --config, no plugin, no env). A field of
type "file" is read INSIDE the jail by the seat side (size-capped) and sent as its content; the broker never
opens a path a seat chose. Who the caller is (--as, --key, --session) is not a field: the seat side accepts
and drops those options (seats were taught to pass them), and the broker injects the seat's own identity from
its launch record (build_argv). The broker validates every request again (validate): unknown command, unknown
field, wrong type or an identity-like field means refused.

Hooks: {"v": 2, "kind": "hook", "event": EVENT, "payload": {...}}; the broker keeps only HOOK_FIELDS of the
payload and sets session_id, transcript_path and cwd itself.

Standard library only: the seat side loads this file by path in `python -I`."""
from __future__ import annotations

import argparse
import os

VERSION = 2
FILE_MAX = 256 * 1024
TEXT_MAX = 64 * 1024
LIST_MAX = 64
IDENTITY = ("as", "key", "session", "name", "agent", "agent_key", "session_id", "config", "env", "argv")

# command -> {field: (type, option)}. type: "str" | "bool" | "list" (nargs) | "repeat" (a repeatable option) |
# "file" | "files" (repeatable
# file option) | ("choice", (...)). option: the CLI option ("--job"), or None for a positional.
# "id" says which identity the broker injects: "key" (--key <session>), "as" (--as <name>), or None.
SCHEMA: dict = {
    "post": {"id": "key", "fields": {"job": ("str", "--job"), "to": ("str", "--to"), "message": ("list", None)},
             "required": ("job", "message")},
    "read": {"id": "key", "fields": {"job": ("str", "--job"), "peek": ("bool", "--peek")}},
    "who": {"id": None, "fields": {"job": ("str", "--job")}},
    "status": {"id": None, "fields": {"job": ("str", "--job"), "all": ("bool", "--all"),
                                      "all_agents": ("bool", "--all-agents")}},
    "wait": {"id": None, "fields": {"job": ("str", "--job"), "for": ("str", "--for"), "until": ("str", "--until"),
                                    "on": ("list", "--on")}, "required": ("job",)},
    "verdict": {"id": "as", "fields": {
        "job": ("str", "--job"), "verdict": (("choice", ("met", "not_met", "unreachable", "show")), None),
        "reason": ("list", None), "artifact": ("str", "--artifact"), "details": ("file", "--details"),
        "scope": (("choice", ("artifact", "goal", "ruling")), "--scope"), "open": ("str", "--open"),
        "progress": (("choice", ("yes",)), "--progress"), "candidate": ("str", "--candidate"),
        "req": ("str", "--req"), "reason_opt": ("str", "--reason"), "next": ("str", "--next")},
        "required": ("job", "verdict")},
    "done": {"id": "key", "fields": {"job": ("str", "--job"), "artifact": ("str", "--artifact"),
                                     "summary": ("str", "--summary"), "branch": ("str", "--branch"),
                                     "sha": ("str", "--sha")}, "required": ("job",)},
    "review": {"id": "key", "fields": {"job": ("str", "--job"), "artifact": ("str", "--artifact"),
                                       "outcome": (("choice", ("verified", "changes")), None),
                                       "report": ("file", "--report")}, "required": ("job", "outcome")},
    "goal-ready": {"id": None, "fields": {"job": ("str", "--job"), "candidate": ("str", "--candidate"),
                                          "add_requirements": ("files", "--add-requirements")},
                   "required": ("job",)},
    "land": {"id": "key", "fields": {"job": ("str", "--job"), "artifact": ("str", "--artifact")},
             "required": ("job",)},
    "ask": {"id": "key", "fields": {"job": ("str", "--job"), "to": ("str", "--to"), "text": ("list", None),
                                    "options": ("str", "--options"), "default": ("str", "--default"),
                                    "expires": ("str", "--expires"), "blocks": ("str", "--blocks")},
            "required": ("job", "to", "text")},
    "answer": {"id": "key", "fields": {"id": ("str", None), "text": ("list", None), "option": ("str", "--option"),
                                       "default": ("bool", "--default"), "comment": ("str", "--comment"),
                                       "reopen": ("str", "--reopen")}, "required": ("id",)},
    "seat request": {"id": None, "fields": {
        "job": ("str", "--job"), "seat": ("str", "--seat"), "role": ("str", "--role"), "brief": ("str", "--brief"),
        "model": ("str", "--model"), "harness": (("choice", ("claude", "codex")), "--harness"),
        "workdir": ("str", "--workdir"), "title": ("str", "--title"), "task": ("str", "--task"),
        "complexity": (("choice", ("simple", "standard", "complex")), "--complexity")},
        "required": ("job", "role", "brief")},
    "seat list": {"id": None, "fields": {"job": ("str", "--job")}},
    "seat show": {"id": None, "fields": {"job": ("str", "--job"), "seat": ("str", "--seat")}},
    "seat release": {"id": None, "fields": {"job": ("str", "--job"), "seat": ("str", "--seat"),
                                            "withdraw_artifact": ("str", "--withdraw-artifact"),
                                            "reason": ("str", "--reason")}},
    "seat ack": {"id": None, "fields": {"job": ("str", "--job"), "upto": ("str", "--upto"),
                                        "event": ("repeat", "--event")}},
}
HOOK_EVENTS = ("session-start", "session-end", "start", "turn", "done", "session-stop", "stop")
GUARD_EVENTS = ("turn",)       # a guard hook that cannot run fails CLOSED (the tool call is denied)
HOOK_FIELDS = ("hook_event_name", "tool_name", "tool_input", "tool_response", "tool_use_id", "agent_id",
               "agent_type", "permission_mode", "stop_hook_active", "source", "model", "turn_id", "reason",
               "message", "prompt", "last_assistant_message", "matcher", "notification_type")


class ProtocolError(ValueError):
    """A request that is not one a seat may send."""


# --- the seat side ----------------------------------------------------------------------------------

class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ProtocolError(message)


def _command_parser(command: str) -> _Parser:
    spec = SCHEMA[command]
    p = _Parser(prog=f"swarm {command}", allow_abbrev=False, add_help=False)
    for opt in ("--as", "--key", "--session"):   # accepted and DROPPED: the broker injects who the seat is
        p.add_argument(opt, dest="_drop" + opt[2:])
    for name, (typ, opt) in spec["fields"].items():
        kw = {}
        if typ == "bool":
            kw = {"action": "store_true"}
        elif typ == "list":
            kw = {"nargs": "+" if name in spec.get("required", ()) or opt else "*"}
        elif typ in ("files", "repeat"):
            kw = {"action": "append"}
        elif isinstance(typ, tuple):
            kw = {"choices": typ[1]}
        if opt:
            p.add_argument(opt, dest=name, **kw)
        else:
            if typ != "list" and name not in spec.get("required", ()):
                kw["nargs"] = "?"
            p.add_argument(name, **kw)
    return p


def _read_file(path: str, cwd: str) -> str:
    """A file named on the seat's command line, read here in the jail (size-capped), as text."""
    full = os.path.join(cwd, os.path.expanduser(path))
    try:
        with open(full, "rb") as fh:
            data = fh.read(FILE_MAX + 1)
    except OSError as exc:
        raise ProtocolError(f"cannot read {path}: {exc.strerror or exc}") from None
    if len(data) > FILE_MAX:
        raise ProtocolError(f"{path} is over {FILE_MAX} bytes")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise ProtocolError(f"{path} is not UTF-8 text") from None


def parse_cli(argv: list[str], cwd: str, stdin=None) -> dict:
    """The typed request for a seat's `swarm ARGV` (ProtocolError when it is not a seat command)."""
    if not argv:
        raise ProtocolError("no command")
    command = argv[0]
    rest = argv[1:]
    if command == "seat":
        if not rest:
            raise ProtocolError("seat: say request, list, show, release or ack")
        command, rest = f"seat {rest[0]}", rest[1:]
    if command not in SCHEMA:
        raise ProtocolError(f"`swarm {command}` is not a command a drive seat runs")
    ns = _command_parser(command).parse_args(rest)
    fields = {}
    for name, (typ, _opt) in SCHEMA[command]["fields"].items():
        v = getattr(ns, name, None)
        if v is None or v is False or v == []:
            continue
        if typ == "file":
            v = (stdin.read(FILE_MAX + 1) if stdin is not None else "") if v == "-" else _read_file(v, cwd)
        elif typ == "files":
            v = [_read_file(x, cwd) for x in v]
        fields[name] = v
    req = {"v": VERSION, "kind": "cli", "command": command, "fields": fields}
    validate(req)
    return req


# --- the broker side --------------------------------------------------------------------------------

def _check_text(name: str, v, limit: int = TEXT_MAX) -> None:
    if not isinstance(v, str) or len(v) > limit or "\x00" in v:
        raise ProtocolError(f"field {name} is not text")


def validate(req) -> tuple[str, dict]:
    """(command, fields) of a well-formed cli request; ProtocolError otherwise. Unknown, identity-like or
    mistyped fields are refused, never ignored."""
    if not isinstance(req, dict) or req.get("v") != VERSION or req.get("kind") != "cli":
        raise ProtocolError("not a v2 cli request")
    if set(req) - {"v", "kind", "command", "fields"}:
        raise ProtocolError("unknown request keys")
    command, fields = req.get("command"), req.get("fields")
    if command not in SCHEMA or not isinstance(fields, dict):
        raise ProtocolError("unknown command")
    spec = SCHEMA[command]
    for name, v in fields.items():
        if name in IDENTITY or name.lstrip("_") in IDENTITY:
            raise ProtocolError(f"field {name} names a caller: the broker sets who the seat is")
        if name not in spec["fields"]:
            raise ProtocolError(f"unknown field {name!r} for {command}")
        typ = spec["fields"][name][0]
        if typ == "bool":
            if v is not True:
                raise ProtocolError(f"field {name} is not a flag")
        elif typ in ("list", "files", "repeat"):
            if not isinstance(v, list) or not v or len(v) > LIST_MAX:
                raise ProtocolError(f"field {name} is not a list")
            for x in v:
                _check_text(name, x, FILE_MAX if typ == "files" else TEXT_MAX)
        elif typ == "file":
            _check_text(name, v, FILE_MAX)
        elif isinstance(typ, tuple):
            if v not in typ[1]:
                raise ProtocolError(f"field {name} is not one of {typ[1]}")
        else:
            _check_text(name, v)
    missing = [r for r in spec.get("required", ()) if r not in fields]
    if missing:
        raise ProtocolError(f"missing {', '.join(missing)}")
    return command, fields


def build_argv(command: str, fields: dict, *, key: str | None, name: str | None, file_path) -> list[str]:
    """The CLI argv the broker runs for a validated request: options first (each value a separate argument
    that is never read as an option: a value starting with "-" is refused), then the seat's identity, then
    positionals after "--". `file_path(content)` stores a file field's content where only the supervisor
    can write and returns that path."""
    spec = SCHEMA[command]
    argv = command.split(" ")
    positional: list[str] = []
    for fname, (typ, opt) in spec["fields"].items():
        if fname not in fields:
            continue
        v = fields[fname]
        if opt is None:
            positional += v if isinstance(v, list) else [v]
            continue
        if typ == "bool":
            argv.append(opt)
        elif typ == "file":
            argv += [opt, file_path(v)]
        elif typ == "files":
            for content in v:
                argv += [opt, file_path(content)]
        elif typ in ("list", "repeat"):
            for x in v:
                if x.startswith("-"):
                    raise ProtocolError(f"field {fname}: a value may not start with '-'")
            argv += [opt, *v] if typ == "list" else [a for x in v for a in (opt, x)]
        else:
            if v.startswith("-"):
                raise ProtocolError(f"field {fname}: a value may not start with '-'")
            argv += [opt, v]
    if spec["id"] == "key":
        if not key:
            raise ProtocolError("the seat's session is not known yet")
        argv += ["--key", key]
    elif spec["id"] == "as":
        if not name:
            raise ProtocolError("the seat's name is not known")
        argv += ["--as", name]
    if positional:
        argv += ["--", *positional]
    return argv
