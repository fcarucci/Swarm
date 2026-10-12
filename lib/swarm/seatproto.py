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
REQUIREMENT_MAX = 1024 * 1024   # drive_cli.REQUIREMENT_MAX
TEXT_MAX = 64 * 1024
LIST_MAX = 64
IDENTITY = ("as", "key", "session", "name", "agent", "agent_key", "session_id", "config", "env", "argv")

# command -> {field: (type, option)}. type:
#   "str"     an identifier (job, artifact, sha, model, ...): no leading "-", no control character;
#   "text"    free text (a summary, a reason, a comment): anything but NUL;
#   "bool"    a flag; "list" free-text words (a positional, or one option value joined by spaces);
#   "repeat"  a repeatable identifier option; ("choice", (...)) one of the values;
#   "file"    a file read in the jail and sent as its content (verdict --details);
#   "report"  a file sent as {"path": as typed, "content": its content}: the post names the path the seat typed,
#             the broker keeps the content (review --report);
#   "paths"   files the BROKER reads from the seat's work tree, without following a link, and pins at the
#             content it read (goal-ready --add-requirements: the pin is the path in the work tree).
# option: the CLI option ("--job"), or None for a positional.
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
        "req": ("str", "--req"), "reason_opt": ("text", "--reason"), "next": ("text", "--next")},
        "required": ("job", "verdict")},
    "done": {"id": "key", "fields": {"job": ("str", "--job"), "artifact": ("str", "--artifact"),
                                     "summary": ("text", "--summary"), "branch": ("str", "--branch"),
                                     "sha": ("str", "--sha")}, "required": ("job",)},
    "review": {"id": "key", "fields": {"job": ("str", "--job"), "artifact": ("str", "--artifact"),
                                       "outcome": (("choice", ("verified", "changes")), None),
                                       "report": ("report", "--report")},
               "required": ("job", "outcome", "report")},
    "goal-ready": {"id": None, "fields": {"job": ("str", "--job"), "candidate": ("str", "--candidate"),
                                          "add_requirements": ("paths", "--add-requirements")},
                   "required": ("job",)},
    "land": {"id": "key", "fields": {"job": ("str", "--job"), "artifact": ("str", "--artifact")},
             "required": ("job",)},
    "ask": {"id": "key", "fields": {"job": ("str", "--job"), "to": ("str", "--to"), "text": ("list", None),
                                    "options": ("text", "--options"), "default": ("text", "--default"),
                                    "expires": ("str", "--expires"), "blocks": ("str", "--blocks")},
            "required": ("job", "to", "text")},
    "answer": {"id": "key", "fields": {"id": ("str", None), "text": ("list", None), "option": ("text", "--option"),
                                       "default": ("bool", "--default"), "comment": ("text", "--comment"),
                                       "reopen": ("text", "--reopen")}, "required": ("id",)},
    "seat request": {"id": None, "fields": {
        "job": ("str", "--job"), "seat": ("str", "--seat"), "role": ("str", "--role"), "brief": ("str", "--brief"),
        "model": ("str", "--model"), "harness": (("choice", ("claude", "codex")), "--harness"),
        "workdir": ("str", "--workdir"), "title": ("text", "--title"), "task": ("text", "--task"),
        "complexity": (("choice", ("simple", "standard", "complex")), "--complexity")},
        "required": ("job", "role", "brief")},
    "seat list": {"id": None, "fields": {"job": ("str", "--job")}},
    "seat show": {"id": None, "fields": {"job": ("str", "--job"), "seat": ("str", "--seat")}},
    "seat release": {"id": None, "fields": {"job": ("str", "--job"), "seat": ("str", "--seat"),
                                            "withdraw_artifact": ("str", "--withdraw-artifact"),
                                            "reason": ("text", "--reason")}},
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
        elif typ in ("paths", "repeat"):
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
        elif typ == "report":
            v = {"path": v, "content": _read_file(v, cwd)}
        elif typ == "paths":
            v = [os.path.normpath(os.path.join(cwd, os.path.expanduser(x))) for x in v]
        fields[name] = v
    req = {"v": VERSION, "kind": "cli", "command": command, "fields": fields}
    validate(req)
    return req


# --- the broker side --------------------------------------------------------------------------------

def _check_text(name: str, v, limit: int = TEXT_MAX) -> None:
    if not isinstance(v, str) or len(v) > limit or "\x00" in v:
        raise ProtocolError(f"field {name} is not text")


def _check_ident(name: str, v) -> None:
    """An identifier value: text with no control character that does not start with "-"."""
    _check_text(name, v)
    if v.startswith("-") or any(ord(c) < 32 or ord(c) == 127 for c in v):
        raise ProtocolError(f"field {name}: an identifier may not start with '-' or hold control characters")


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
        elif typ in ("list", "paths", "repeat"):
            if not isinstance(v, list) or not v or len(v) > LIST_MAX:
                raise ProtocolError(f"field {name} is not a list")
            for x in v:
                if typ == "list":
                    _check_text(name, x)
                else:
                    _check_ident(name, x)
                if typ == "paths" and not os.path.isabs(x):
                    raise ProtocolError(f"field {name}: {x!r} is not an absolute path")
        elif typ == "file":
            _check_text(name, v, FILE_MAX)
        elif typ == "report":
            if not isinstance(v, dict) or set(v) != {"path", "content"}:
                raise ProtocolError(f"field {name} is not a report")
            _check_ident(name, v["path"])
            _check_text(name, v["content"], FILE_MAX)
        elif isinstance(typ, tuple):
            if v not in typ[1]:
                raise ProtocolError(f"field {name} is not one of {typ[1]}")
        elif typ == "text":
            _check_text(name, v)
        else:
            _check_ident(name, v)
    missing = [r for r in spec.get("required", ()) if r not in fields]
    if missing:
        raise ProtocolError(f"missing {', '.join(missing)}")
    return command, fields


def build_argv(command: str, fields: dict, *, key: str | None, name: str | None, file_path,
               report=None, requirement=None) -> list[str]:
    """The CLI argv the broker runs for a validated request: every option as ONE `--opt=value` argument (argparse
    never reads a value as an option), then the seat's identity, then positionals after "--". `file_path(content)`
    stores a file field's content where only the supervisor can write and returns that path; `report(path,
    content)` and `requirement(path)` give the arguments of a report and of each requirement (the broker's)."""
    spec = SCHEMA[command]
    argv = command.split(" ")
    positional: list[str] = []
    for fname, (typ, opt) in spec["fields"].items():
        if fname not in fields:
            continue
        v = fields[fname]
        if opt is None:
            positional += v if isinstance(v, list) else [v]
        elif typ == "bool":
            argv.append(opt)
        elif typ == "file":
            argv.append(f"{opt}={file_path(v)}")
        elif typ == "report":
            if report is None:
                raise ProtocolError(f"{command}: no report handler")
            argv += report(v["path"], v["content"])
        elif typ == "paths":
            if requirement is None:
                raise ProtocolError(f"{command}: no requirement handler")
            for path in v:
                argv += requirement(path)
        elif typ == "list":
            argv.append(f"{opt}={' '.join(v)}")
        elif typ == "repeat":
            argv += [f"{opt}={x}" for x in v]
        else:
            argv.append(f"{opt}={v}")
    if spec["id"] == "key":
        if not key:
            raise ProtocolError("the seat's session is not known yet")
        argv.append(f"--key={key}")
    elif spec["id"] == "as":
        if not name:
            raise ProtocolError("the seat's name is not known")
        argv.append(f"--as={name}")
    if positional:
        argv += ["--", *positional]
    return argv
