"""Claude Code and Codex JSONL transcripts as readable turns (for `swarm transcript show`).

A transcript is one JSON object per line. Claude: the entries that matter are `user` and
`assistant` messages; their content is a string or a list of blocks (text, tool_use, tool_result,
thinking, image). Everything else (mode, attachment, file-history-snapshot, titles, ...) is
session bookkeeping and is left out, as are thinking blocks and meta user entries. Codex: a
rollout (`_is_codex`, by its first lines' top-level `type`) is one `response_item` per event --
message (user/assistant), function_call/custom_tool_call and their _output, everything else
(session_meta, event_msg, turn_context, reasoning/developer/context items) skipped. Either way, a
line that is not JSON is kept as an "unparsed" turn, so nothing silently disappears, and the
swarm's own marker for a transcript cut to its head and tail (an entry whose type starts with
"swarm") shows as a "truncated" turn.

Stdlib only.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
from dataclasses import dataclass

from swarm.hosts.codex import CONTEXT_PREFIXES as _CONTEXT   # one definition; stdlib-only module
from swarm.textsafe import term_safe

# A tool result longer than this is cut to its head and tail with a note in between.
TRIM_LINES = 30
HEAD_LINES = 12
TAIL_LINES = 8
TRIM_CHARS = 2000
# Tool-call inputs are shown as JSON, cut to this many characters.
INPUT_CHARS = 1000


@dataclass
class Turn:
    kind: str            # user | assistant | tool call | tool result | memory saved | truncated | unparsed
    text: str            # what is shown (tool results trimmed)
    full: str            # the untrimmed text, which --grep searches
    ts: str | None = None
    label: str = ""      # the tool's name, for tool calls and results
    error: bool = False  # a tool result flagged is_error
    call_id: str = ""    # the tool call's id, for tool calls and results (both hosts)


def _trim(text: str) -> str:
    lines = text.splitlines()
    if len(lines) > TRIM_LINES:
        cut = len(lines) - HEAD_LINES - TAIL_LINES
        lines = lines[:HEAD_LINES] + [f"[… {cut} lines trimmed …]"] + lines[-TAIL_LINES:]
        text = "\n".join(lines)
    if len(text) > TRIM_CHARS:
        head, tail = TRIM_CHARS * 2 // 3, TRIM_CHARS // 4
        text = f"{text[:head]}\n[… {len(text) - head - tail} chars trimmed …]\n{text[-tail:]}"
    return text


def human_size(n: int | float | None) -> str:
    """Bytes for people: "512 B", "1.5 KB", "12.3 MB", "2.0 GB"."""
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{int(n)} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return ""  # unreachable


def _placeholder(obj):
    """The first swarm-image placeholder (see transcripts.extract_images) inside obj, or None."""
    if isinstance(obj, dict):
        if obj.get("type") == "swarm-image" and "sha256" in obj:
            return obj
        values = obj.values()
    elif isinstance(obj, list):
        values = obj
    else:
        return None
    for v in values:
        found = _placeholder(v)
        if found is not None:
            return found
    return None


def image_label(block) -> str:
    """`[image <mime> <size> sha256:<12>]` for an image block whose data the archive holds
    apart; `[image]` for one still inline."""
    ph = _placeholder(block)
    if ph is None:
        return "[image]"
    return f"[image {ph.get('mime', '?')} {human_size(ph.get('bytes'))} sha256:{str(ph['sha256'])[:12]}]"


def _block_text(content) -> str:
    """Text of a tool_result's content: a string, or a list of text/image blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(str(b.get("text", "")))
            elif isinstance(b, dict) and b.get("type") == "image":
                parts.append(image_label(b))
            elif isinstance(b, dict):
                parts.append(f"[{b.get('type', 'block')}]")
            else:
                parts.append(str(b))
        return "\n".join(parts)
    return "" if content is None else json.dumps(content, ensure_ascii=False)


def _tool_input(inp) -> str:
    text = json.dumps(inp, ensure_ascii=False, indent=None) if not isinstance(inp, str) else inp
    return text if len(text) <= INPUT_CHARS else text[:INPUT_CHARS] + f"… [{len(text) - INPUT_CHARS} chars trimmed]"


# --------------------------------------------------------------------------- memory provenance

# The anchor line of a memory reference's excerpt (swarm.provenance.anchor_line): which memories
# the call saved, and its output.
MEMORY_TYPE = "swarm-memory"


def _memory_turn(e: dict) -> Turn:
    mems = [m for m in e.get("memories") or [] if isinstance(m, dict)]
    head = ", ".join(f"{m.get('document_id')} (bank {m.get('bank')}, {m.get('writer')})" for m in mems) or "?"
    out = str(e.get("output") or "").strip()
    text = head + (f"\n{_trim(out)}" if out else "")
    ts = e.get("timestamp")
    return Turn("memory saved", text, head + "\n" + out, ts if isinstance(ts, str) else None, "",
                call_id=str(e.get("tool_call_id") or ""))


def _parse_ts(ts):
    try:
        t = _dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo is not None else t.replace(tzinfo=_dt.timezone.utc)


def anchor_index(items: list[Turn], tool_call_id: str | None, at) -> int | None:
    """Where a memory was saved: the tool call with that id (Claude; Codex outside code mode),
    else the last tool call at or before `at` (Codex code mode); None if neither."""
    if tool_call_id:
        for i in range(len(items) - 1, -1, -1):
            if items[i].kind == "tool call" and items[i].call_id == tool_call_id:
                return i
    if at is None:
        return None
    if isinstance(at, _dt.datetime) and at.tzinfo is None:
        at = at.replace(tzinfo=_dt.timezone.utc)      # as _parse_ts reads a naive turn time
    best = None
    for i, t in enumerate(items):
        ts = _parse_ts(t.ts) if t.kind == "tool call" and t.ts else None
        if ts is not None and ts <= at:
            best = i
    return best


def mark_memories(items: list[Turn], refs) -> list[Turn]:
    """The turns with a "memory saved" marker after each memory write's call (and its result),
    for `swarm transcript show`. A ref whose call isn't found gets no marker."""
    inserts = []
    for r in refs or ():
        i = anchor_index(items, r.tool_call_id, r.created_at)
        if i is None:
            continue
        pos = i + 1
        if pos < len(items) and items[pos].kind == "tool result" and items[pos].call_id == items[i].call_id:
            pos += 1
        text = f"{r.document_id} (bank {r.bank}, {r.writer}) · swarm transcript show --memory {r.document_id}"
        inserts.append((pos, Turn("memory saved", text, text, items[i].ts)))
    out = list(items)
    for pos, t in sorted(inserts, key=lambda x: x[0], reverse=True):
        out.insert(pos, t)
    return out


# --------------------------------------------------------------------------- Codex rollouts

_CODEX_TYPES = ("session_meta", "response_item", "event_msg", "turn_context")


def _is_codex(jsonl: str) -> bool:
    """Whether this looks like a Codex rollout, not a Claude Code transcript: one of its first
    few lines has a Codex-only top-level `type`."""
    for line in jsonl.splitlines()[:5]:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict) and e.get("type") in _CODEX_TYPES:
            return True
    return False


# Codex content items (codex-rs/protocol at rust-v0.157.1): message content is input_text /
# output_text / input_image; a tool output is a string, or ContentItems: input_text {text},
# input_image {image_url, detail} (view_image), possibly wrapped as {"content": [...]}.
_CODEX_TEXT = ("input_text", "output_text", "text")
_CODEX_IMAGE = ("input_image", "image", "swarm-image")


def _codex_item_text(i) -> str:
    if not isinstance(i, dict):
        return str(i)
    kind = i.get("type")
    if kind in _CODEX_TEXT:
        return str(i.get("text", ""))
    if kind in _CODEX_IMAGE or "image_url" in i:
        return "[image]"
    return f"[{kind or 'item'}]"


def _codex_output_text(o) -> str:
    if o is None:
        return ""
    if isinstance(o, str):
        return o
    if isinstance(o, dict):
        if "content" in o:
            return _codex_output_text(o["content"])
        return _codex_item_text(o)
    if isinstance(o, list):
        return "\n".join(_codex_item_text(i) for i in o)
    return json.dumps(o, ensure_ascii=False)


def _codex_turns(jsonl: str) -> list[Turn]:
    out: list[Turn] = []
    calls: dict[str, str] = {}   # call_id -> tool name
    for line in jsonl.splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            if line.strip():
                out.append(Turn("unparsed", line, line))
            continue
        if not isinstance(e, dict):
            continue
        ts, p = e.get("timestamp"), e.get("payload") or {}
        if e.get("type") == MEMORY_TYPE:
            out.append(_memory_turn(e))
            continue
        if str(e.get("type", "")).startswith("swarm"):
            text = ", ".join(f"{k}: {v}" for k, v in e.items() if k != "type")
            out.append(Turn("truncated", text, text, ts))
            continue
        if e.get("type") != "response_item":
            continue   # session_meta, event_msg, turn_context: bookkeeping, not shown
        pt = p.get("type")
        if pt == "message" and p.get("role") in ("user", "assistant"):
            for c in p.get("content") or []:
                if not isinstance(c, dict):
                    continue
                if c.get("type") in ("input_text", "output_text", "text"):
                    t = str(c.get("text", ""))
                    if t.strip() and not t.lstrip().startswith(_CONTEXT):
                        out.append(Turn(p["role"], t, t, ts))
                elif c.get("type") in _CODEX_IMAGE or "image_url" in c:
                    out.append(Turn(p["role"], "[image]", "[image]", ts))
        elif pt in ("function_call", "custom_tool_call"):
            name = str(p.get("name") or "?")
            calls[str(p.get("call_id"))] = name
            raw = p.get("arguments") if pt == "function_call" else p.get("input")
            full = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
            inp = raw
            if pt == "function_call" and isinstance(raw, str):
                try:
                    inp = json.loads(raw)
                except ValueError:
                    inp = raw
            out.append(Turn("tool call", _tool_input(inp), full, ts, name, call_id=str(p.get("call_id") or "")))
        elif pt in ("function_call_output", "custom_tool_call_output"):
            full = _codex_output_text(p.get("output"))
            out.append(Turn("tool result", _trim(full), full, ts, calls.get(str(p.get("call_id")), ""),
                            call_id=str(p.get("call_id") or "")))
    return out


def turns(jsonl: str) -> list[Turn]:
    """The transcript's turns, in order."""
    if _is_codex(jsonl):
        return _codex_turns(jsonl)
    out: list[Turn] = []
    tools: dict[str, str] = {}   # tool_use id -> tool name
    for line in jsonl.splitlines():
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except ValueError:
            out.append(Turn("unparsed", line, line))
            continue
        if not isinstance(e, dict):
            out.append(Turn("unparsed", line, line))
            continue
        kind, ts = str(e.get("type") or ""), e.get("timestamp")
        if kind == MEMORY_TYPE:
            out.append(_memory_turn(e))
            continue
        if kind.startswith("swarm"):
            text = ", ".join(f"{k}: {v}" for k, v in e.items() if k != "type") or kind
            out.append(Turn("truncated", text, text, ts))
            continue
        if kind not in ("user", "assistant") or e.get("isMeta"):
            continue
        content = (e.get("message") or {}).get("content")
        if isinstance(content, str):
            if content.strip():
                out.append(Turn(kind, content, content, ts))
            continue
        for b in content if isinstance(content, list) else []:
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt == "text" and str(b.get("text", "")).strip():
                out.append(Turn(kind, b["text"], b["text"], ts))
            elif bt == "tool_use":
                name = str(b.get("name") or "?")
                tools[str(b.get("id"))] = name
                full = json.dumps(b.get("input"), ensure_ascii=False)
                out.append(Turn("tool call", _tool_input(b.get("input")), full, ts, name,
                                call_id=str(b.get("id") or "")))
            elif bt == "tool_result":
                full = _block_text(b.get("content"))
                out.append(Turn("tool result", _trim(full), full, ts,
                                tools.get(str(b.get("tool_use_id")), ""), bool(b.get("is_error")),
                                call_id=str(b.get("tool_use_id") or "")))
            elif bt == "image":
                label = image_label(b)
                out.append(Turn(kind, label, label, ts))
    return out


def select(items: list[Turn], tail: int | None = None, grep: str | None = None) -> list[Turn]:
    """Turns matching `grep` (a case-insensitive regex, searched in the untrimmed text and the
    tool name), then the last `tail` of them."""
    if grep:
        rx = re.compile(grep, re.IGNORECASE)
        items = [t for t in items if rx.search(t.full) or rx.search(t.label)]
    return items[-tail:] if tail else items


def select_lines(jsonl: str, tail: int | None = None, grep: str | None = None) -> list[str]:
    """The raw JSONL lines, filtered and cut like `select`."""
    lines = [l for l in jsonl.splitlines() if l.strip()]
    if grep:
        rx = re.compile(grep, re.IGNORECASE)
        lines = [l for l in lines if rx.search(l)]
    return lines[-tail:] if tail else lines


def _clock(ts: str | None) -> str:
    if not ts:
        return "--:--:--"
    try:
        return _dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%H:%M:%S")
    except ValueError:
        return "--:--:--"


# --------------------------------------------------------------------------- colour (TTY only)

# One header colour per turn kind (tool result picks red instead of cyan when it's an error); a
# kind with none (truncated, unparsed) is left plain. Bold+magenta is used for a tool call's own
# name, so it stands out inside the magenta header.
_KIND_SGR = {"user": "1;34", "assistant": "1;32", "tool call": "35", "memory saved": "33"}
_TOOL_NAME_SGR = "1;35"

# Our own markers inside an already textsafe'd body: a redaction placeholder ("[REDACTED:kind]",
# swarm.transcripts.redact) highlighted yellow, an image placeholder ("[image ...]" / "[image]",
# image_label above) dimmed. Never applied to raw content the sanitiser hasn't already run over.
_REDACTED_RE = re.compile(r"\[REDACTED:[\w-]+\]")
_IMAGE_PLACEHOLDER_RE = re.compile(r"\[image(?: [^\]\n]*)?\]")
# A tool call's JSON keys (a quoted string right before a colon), lightly dimmed; the rest of the
# JSON, and every other turn's text, stays plain -- see render_text's docstring.
_JSON_KEY_RE = re.compile(r'"(?:[^"\\]|\\.)*"(?=\s*:)')


def _sgr(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if text else text


def _paint_markers(text: str) -> str:
    text = _REDACTED_RE.sub(lambda m: _sgr("33", m.group()), text)
    return _IMAGE_PLACEHOLDER_RE.sub(lambda m: _sgr("2", m.group()), text)


def _paint_json_keys(text: str) -> str:
    return _JSON_KEY_RE.sub(lambda m: _sgr("2", m.group()), text)


def render_text(items: list[Turn], color: bool = False) -> str:
    """Turns as `── HH:MM:SS <kind>[: tool][ (error)]` headers, each followed by its text. The
    text is agent-written: every terminal control in it (and in the tool name) is shown as
    visible notation (textsafe.term_safe) *before* any colour is added, so nothing from
    transcript content can smuggle an escape sequence past our own; its own newlines are kept.

    With color (a TTY, not --jsonl, not NO_COLOR/--no-color; render_text itself never checks the
    environment -- that's the caller's job): the clock is dimmed; the kind is coloured per
    _KIND_SGR (tool result: red instead of cyan when `error`); a tool call's own tool name is
    bold+magenta. Inside the body, a tool call's JSON keys are dimmed and our own redaction/image
    placeholders (_paint_markers) are highlighted; the rest of the body is never coloured."""
    blocks = []
    for t in items:
        kind_sgr = ("31" if t.error else "36") if t.kind == "tool result" else _KIND_SGR.get(t.kind)
        clock = _sgr("2", _clock(t.ts)) if color else _clock(t.ts)
        kind_text = term_safe(t.kind)
        label_text = term_safe(t.label) if t.label else ""
        if color and kind_sgr:
            kind_part = _sgr(kind_sgr, kind_text)
            label_part = _sgr(_TOOL_NAME_SGR if t.kind == "tool call" else kind_sgr, label_text)
            error_part = _sgr(kind_sgr, " (error)") if t.error else ""
        else:
            kind_part, label_part = kind_text, label_text
            error_part = " (error)" if t.error else ""
        head = f"── {clock} {kind_part}" + (f": {label_part}" if label_text else "") + error_part
        body = term_safe(t.text.rstrip("\n"), keep_newlines=True)
        if color:
            if t.kind == "tool call":
                body = _paint_json_keys(body)
            body = _paint_markers(body)
        blocks.append(head + "\n" + body)
    return "\n\n".join(blocks)


# What json_safe_line escapes: the characters term_safe makes visible (tab kept, newline excluded: a
# JSONL line has none).
_JSON_UNSAFE = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\u2028\u2029\u202a-\u202e\u2066-\u2069\ud800-\udfff]")


def json_safe_line(line: str) -> str:
    """A JSONL line with every terminal control written as a JSON \\uNNNN escape: a valid JSON
    line keeps its values (inside a string the escape means the same character, and JSON has
    no raw controls outside strings), and a line that isn't JSON shows the escapes."""
    return _JSON_UNSAFE.sub(lambda m: f"\\u{ord(m.group()):04x}", line)
