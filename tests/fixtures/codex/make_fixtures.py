"""Build tests/fixtures/codex/0.157.1 from a raw Codex capture, keeping only what the
swarm's Codex adapter and viewer read and replacing every content-bearing value. Usage (from the
repo root): python3 tests/fixtures/codex/make_fixtures.py ~/src/swarm-notes/codex-fixture/out

The capture ran multi-agent V2 (ANSWERS.md Q1): hook tool names carry the "collaboration"
namespace, spawn_agent/followup_task messages are ciphertext ("gAAAAA..."), the child receives its
task as an agent_message (plaintext header + encrypted_content), and tools run inside code-mode
`exec` custom tool calls. Kept as structure: task names and agent paths (the fixture's own names),
enum-like values, tool names; replaced: ids, paths, text, tool arguments and outputs, ciphertext,
the image. Dropped: everything not allowlisted (creator_user_id/creator_account_id among it).

PROVENANCE: the raw capture this fixture is built from used code-mode
`exec` for every tool call, so it has no distinct `view_image`/`apply_patch` response_items --
The tests need both as separate tool-call labels. Rather than have this script
fabricate them (it only ever replays a real capture's structure), one `view_image` and one
`apply_patch` call+output pair were added BY HAND directly to the already-generated
tests/fixtures/codex/0.157.1/rollouts/root.jsonl (call_synthetic_021/022, same synthetic style
and ids as every other entry there). Each item's shape was checked against the Codex source at
tag rust-v0.157.1 (github.com/openai/codex), not guessed:
  - `view_image` function_call: name "view_image" (core/src/tools/handlers/view_image.rs:74,
    `ToolName::plain("view_image")`), arguments `{"path": ...}` (ViewImageArgs, view_image.rs:58-64:
    `path`, optional `environment_id`, optional `detail`).
  - `view_image` function_call_output: a single `input_image` content item -- no sibling
    `input_text` item (view_image.rs:239-254, `to_response_item`: `FunctionCallOutputBody::
    ContentItems(vec![FunctionCallOutputContentItem::InputImage{ image: Inline{image_url}, detail:
    Some(image_detail) }])`); `image_url` is a `data:application/octet-stream;base64,...` URL
    (view_image.rs:201, `data_url_from_bytes("application/octet-stream", &file_bytes)`); the
    `InputImage` variant flattens `ImageReference::Inline{image_url}` alongside `detail`
    (protocol/src/models.rs:2101-2107); `detail` defaults to `DEFAULT_IMAGE_DETAIL` = High, so
    `"detail": "high"` (protocol/src/models.rs:928-935, `#[serde(rename_all = "lowercase")]`).
  - `apply_patch` custom_tool_call (not function_call: apply_patch is a freeform/custom tool):
    `ToolPayload::Custom { input }` (apply_patch.rs:376) -> rollout item `ResponseItem::
    CustomToolCall { call_id, name, input, .. }` (protocol/src/models.rs:1133-1147) -- `input` is
    the raw patch text itself, not JSON (matches the fixture's existing `exec` items, whose
    `input` is likewise a raw string, not `arguments`).
  - `apply_patch` custom_tool_call_output: `output` is a plain JSON string, not an array/object --
    `ApplyPatchToolOutput::to_response_item` (core/src/tools/context.rs:326-333) builds a single
    `FunctionCallOutputContentItem::InputText` item, and `function_tool_response`
    (core/src/tools/context.rs:578-600) collapses a lone `InputText` item to
    `FunctionCallOutputBody::Text(text)` -- which `FunctionCallOutputPayload`'s custom `Serialize`
    (protocol/src/models.rs:2249-2260) writes as a bare string -- and routes to
    `ResponseInputItem::CustomToolCallOutput` (not `FunctionCallOutput`) because the tool's own
    payload was `ToolPayload::Custom`.
Sparse-clone used to verify: `git clone --depth 1 --branch rust-v0.157.1 --filter=blob:none
--sparse https://github.com/openai/codex`, then `git sparse-checkout set codex-rs/protocol
codex-rs/core/src/tools/handlers` (add `codex-rs/core/src/tools/context.rs`'s directory too)."""
from __future__ import annotations

import base64
import glob
import json
import re
import shutil
import struct
import sys
import zlib
from pathlib import Path

SRC = Path(sys.argv[1]).expanduser() if __name__ == "__main__" else None   # importable for SYNTH_B64
DST = Path(__file__).resolve().parent / "0.157.1"
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
TAG = re.compile(r"^\[swarm [a-z]+:.*\]$")
HEADER = re.compile(r"^(Message Type|Task name|Sender): \S+$|^Payload:$")
CIPHER = "gAAAAA-synthetic-ciphertext"
CONTEXT = ("<environment_context>", "<user_instructions>", "<permissions", "# AGENTS.md", "<INSTRUCTIONS>")
# (the same tuple as swarm.hosts.codex.CONTEXT_PREFIXES; tests/test_codex_host.py checks they agree)


def synth_png(size: int = 32) -> bytes:
    """A deterministic 32x32 RGB PNG (~3 KB, stored uncompressed): big enough for extract_images."""
    raw = b"".join(b"\x00" + bytes(v for x in range(size) for v in (x * 8 % 256, y * 8 % 256, (x + y) * 4 % 256))
                   for y in range(size))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 0)) + chunk(b"IEND", b""))


SYNTH_B64 = base64.b64encode(synth_png()).decode("ascii")
SYNTH_HEX = "0123456789abcdef" * 2          # stands in for any captured 32-hex-digit identifier
HEX_RUN = re.compile(r"[0-9a-f]{32,}")


def hex_leaks(root: Path) -> list[tuple[str, str]]:
    """(file, run) for every run of 32+ lowercase hex digits under root that isn't SYNTH_HEX: a
    captured identifier (connector ids, hashes) that the rebuild failed to replace."""
    return [(str(f.relative_to(root)), run) for f in sorted(root.rglob("*")) if f.is_file()
            for run in HEX_RUN.findall(f.read_text(errors="replace")) if run != SYNTH_HEX]


def synth_data_url(orig: str) -> str:
    """The same data-URL prefix as the captured one (its media type matters to extract_images),
    with the synthetic PNG as the data."""
    prefix = orig.split(",", 1)[0] + "," if orig.startswith("data:") and "," in orig else "data:image/png;base64,"
    return prefix + SYNTH_B64


def synth_value(v, key: str | None = None):
    """Keep the structure of a tool input/output; data URLs become the synthetic image,
    "type"/"detail" enum values stay, every other string becomes "synthetic"."""
    if isinstance(v, dict):
        return {k: synth_value(x, k) for k, x in v.items()}
    if isinstance(v, list):
        return [synth_value(x) for x in v]
    if isinstance(v, str):
        if v.startswith("data:"):
            return synth_data_url(v)
        return v if key in ("type", "detail") else "synthetic"
    return v


HOOK_KEYS = ("hook_event_name", "session_id", "agent_id", "agent_type", "turn_id", "model", "permission_mode",
             "source", "tool_name", "tool_use_id", "transcript_path", "agent_transcript_path", "cwd",
             "stop_hook_active", "last_assistant_message")
COLLAB_TOOLS = ("spawn_agent", "followup_task", "send_input")
COLLAB_KEYS = ("task_name", "fork_turns", "target", "message", "items", "agent_type", "model")
_ids: dict[str, str] = {}
_calls: dict[str, str] = {}


def fake_id(real: str) -> str:
    return _ids.setdefault(real, f"00000000-0000-4000-8000-{len(_ids) + 1:012d}")


def fake_ids(s: str) -> str:
    return UUID.sub(lambda m: fake_id(m.group(0)), s)


def fake_call(real) -> str | None:
    """Tool call ids (call_..., not UUIDs): sequential fakes, so Pre/Post pairs still match."""
    return _calls.setdefault(real, f"call_synthetic_{len(_calls) + 1:03d}") if isinstance(real, str) else None


def fake_path(p) -> str | None:
    m = UUID.search(p or "") if isinstance(p, str) else None
    return f"/home/alice/.codex/sessions/2026/09/26/rollout-2026-09-26T00-00-00-{fake_id(m.group(0))}.jsonl" if m else None


def synth_text(text: str) -> str:
    s = text.lstrip()
    for prefix in CONTEXT:
        if s.startswith(prefix):
            return f"{prefix} synthetic context"
    tags = [l.strip() for l in text.splitlines() if TAG.match(l.strip())]
    return "\n".join(tags + ["synthetic message body"])


def synth_message(v: str) -> str:
    """A spawn/follow-up message: V2 ciphertext stays recognisably ciphertext, plaintext (V1) keeps
    only its tag lines."""
    return CIPHER if v.startswith("gAAAAA") else synth_text(v)


def synth_collab(ti: dict) -> dict:
    out = {}
    for k in COLLAB_KEYS:
        if k not in ti:
            continue
        v = ti[k]
        if k == "message" and isinstance(v, str):
            out[k] = synth_message(v)
        elif k == "items" and isinstance(v, list):
            out[k] = [{"type": i.get("type", "text"), "text": synth_text(str(i.get("text", "")))} for i in v if isinstance(i, dict)]
        elif k in ("task_name", "target", "fork_turns", "model") and isinstance(v, str):
            out[k] = v                     # the fixture's own names/agent paths, enum, model name
        else:
            out[k] = "default"
    return out


def is_collab(name) -> bool:
    return isinstance(name, str) and name.endswith(COLLAB_TOOLS)


def hook(d: dict) -> dict:
    out = {}
    for k in HOOK_KEYS:
        if k not in d:
            continue
        v = d[k]
        if k in ("session_id", "agent_id", "turn_id"):
            out[k] = fake_id(v) if isinstance(v, str) and UUID.fullmatch(v) else f"synthetic-{k}"
        elif k in ("transcript_path", "agent_transcript_path"):
            out[k] = fake_path(v)
        elif k == "tool_use_id":
            out[k] = fake_call(v)
        elif k == "cwd":
            out[k] = "/home/alice/work"
        elif k == "last_assistant_message":
            out[k] = "synthetic"
        else:
            out[k] = v
    for k in ("tool_input", "tool_response"):
        if k in d:
            v = d[k]
            out[k] = synth_collab(v) if k == "tool_input" and is_collab(d.get("tool_name")) and isinstance(v, dict) \
                else synth_value(v)       # keeps str-vs-list-vs-dict
    return out


def meta_source(src):
    """session_meta.payload.source with fake ids and a synthetic nickname."""
    src = json.loads(fake_ids(json.dumps(src)))
    ts = (src.get("subagent") or {}).get("thread_spawn") if isinstance(src, dict) else None
    if isinstance(ts, dict) and ts.get("agent_nickname"):
        ts["agent_nickname"] = "Synthetic"
    return src


def agent_message_content(content) -> list:
    out = []
    for c in content or []:
        if not isinstance(c, dict):
            continue
        if c.get("type") == "encrypted_content":
            out.append({"type": "encrypted_content", "encrypted_content": CIPHER})
        elif isinstance(c.get("text"), str):
            head, sep, body = c["text"].partition("Payload:\n")
            lines = [l for l in (head + ("Payload:" if sep else "")).splitlines() if HEADER.match(l)]
            text = "\n".join(lines) + ("\n" if sep else "")
            if body.strip():
                text += synth_text(body)
            out.append({"type": c.get("type", "input_text"), "text": text})
    return out


def rollout_line(e: dict) -> dict | None:
    t, p = e.get("type"), e.get("payload") or {}
    base = {"timestamp": e.get("timestamp"), "type": t}
    if isinstance(e.get("ordinal"), int):
        base["ordinal"] = e["ordinal"]         # a forked child's own history starts at an ordinal
    if t == "session_meta":
        meta = {"id": fake_id(p["id"]) if p.get("id") else None,
                "session_id": fake_id(p["session_id"]) if isinstance(p.get("session_id"), str) else None,
                "cli_version": p.get("cli_version"), "originator": p.get("originator"),
                "cwd": "/home/alice/work", "source": meta_source(p.get("source"))}
        if isinstance(p.get("forked_from_id"), str):
            meta["forked_from_id"] = fake_id(p["forked_from_id"])
        if isinstance(p.get("subagent_history_start_ordinal"), int):
            meta["subagent_history_start_ordinal"] = p["subagent_history_start_ordinal"]
        return {**base, "payload": meta}
    if t != "response_item":
        return None
    pt = p.get("type")
    if pt == "message" and p.get("role") in ("user", "assistant"):
        content = []
        for c in p.get("content") or []:
            if not isinstance(c, dict):
                continue
            if c.get("type") in ("input_text", "output_text", "text"):
                content.append({"type": c["type"], "text": synth_text(str(c.get("text", "")))})
            elif c.get("type") in ("input_image", "image"):
                item = {"type": c["type"], "image_url": synth_data_url(str(c.get("image_url", "")))}
                if "detail" in c:
                    item["detail"] = c["detail"]
                content.append(item)
        return {**base, "payload": {"type": "message", "role": p["role"], "content": content}}
    if pt == "agent_message":
        return {**base, "payload": {"type": "agent_message", "author": p.get("author"), "recipient": p.get("recipient"),
                                    "content": agent_message_content(p.get("content"))}}
    if pt in ("function_call", "custom_tool_call"):
        name = p.get("name")
        if pt == "function_call" and name in COLLAB_TOOLS:
            try:
                args = json.loads(p.get("arguments") or "{}")
            except ValueError:
                args = {}
            arg = json.dumps(synth_collab(args))
        elif pt == "function_call":
            arg = json.dumps({"synthetic": True})
        else:
            arg = "synthetic"             # a code-mode `exec` script
        key = "arguments" if pt == "function_call" else "input"
        out = {"type": pt, "name": name, "call_id": fake_call(p.get("call_id")), key: arg}
        if p.get("namespace"):
            out["namespace"] = p["namespace"]
        return {**base, "payload": out}
    if pt in ("function_call_output", "custom_tool_call_output"):
        out = p.get("output")
        return {**base, "payload": {"type": pt, "call_id": fake_call(p.get("call_id")),
                                    "output": "synthetic output" if isinstance(out, str) or out is None
                                    else synth_value(out)}}
    return None


def has_image(lines: list[dict]) -> bool:
    return any((l.get("payload") or {}).get("type") in ("function_call_output", "custom_tool_call_output")
               and "base64," in json.dumps(l["payload"]) for l in lines)


def rollout(text: str, need_image: bool = False) -> str:
    lines = []
    for line in text.splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        m = rollout_line(e) if isinstance(e, dict) else None
        if m is not None:
            lines.append(m)
    if need_image and not has_image(lines):
        raise SystemExit("the capture's root rollout has no image output: re-capture with an image in the root rollout")
    return "\n".join(json.dumps(l) for l in lines) + "\n"


def find_key(node, key):
    if isinstance(node, dict):
        if key in node:
            return node[key]
        for v in node.values():
            hit = find_key(v, key)
            if hit is not None:
                return hit
    return None


def answers(text: str) -> str:
    """ANSWERS.md with fake ids, capture file names reduced to their event, hex identifiers and
    the nickname replaced."""
    text = fake_ids(text).replace(str(SRC), "<capture>")
    text = re.sub(r"\b\d{15,}-([A-Za-z]+)-\d+\b", r"<\1 hook>", text)
    text = re.sub(r"\bids-([a-z]+)\.txt\b", r"<\1 shell ids>", text)
    text = HEX_RUN.sub(SYNTH_HEX, text)      # e.g. the P line's plugin_connector_1p_<32 hex>
    return re.sub(r'"agent_nickname": "[^"]*"', '"agent_nickname": "Synthetic"', text)


def main() -> None:
    shutil.rmtree(DST, ignore_errors=True)
    (DST / "hooks").mkdir(parents=True)
    (DST / "rollouts").mkdir()
    counts: dict[str, int] = {}
    for f in sorted(glob.glob(str(SRC / "hooks/*.json"))):
        ev = Path(f).name.split("-")[-2]
        counts[ev] = counts.get(ev, 0) + 1
        stem = f"{ev}-{counts[ev]:02d}"
        (DST / "hooks" / f"{stem}.json").write_text(json.dumps(hook(json.load(open(f))), indent=1) + "\n")
        tr = f[:-len(".json")] + ".transcript.jsonl"
        if Path(tr).exists():
            (DST / "hooks" / f"{stem}.transcript.jsonl").write_text(rollout(open(tr).read()))
    names = {}
    for f in sorted(glob.glob(str(SRC / "sessions/**/rollout-*.jsonl"), recursive=True)):
        src = json.loads(open(f).readline()).get("payload", {}).get("source")   # its own session_meta
        if isinstance(src, str):
            role = "root"
        else:
            role = "grandchild" if (find_key(src, "depth") or 0) >= 2 else "child"
        names[role] = Path(fake_path(f)).name
        (DST / "rollouts" / f"{role}.jsonl").write_text(rollout(open(f).read(), need_image=(role == "root")))
    (DST / "rollouts" / "names.json").write_text(json.dumps(names, indent=1) + "\n")
    ids = {}
    for f in sorted(glob.glob(str(SRC / "ids-*.txt"))):
        who, sess, thread = open(f).read().split()[:3]
        ids[who] = {"session": fake_id(sess), "thread": fake_id(thread)}
    (DST / "ids.json").write_text(json.dumps(ids, indent=1) + "\n")
    (DST / "ANSWERS.md").write_text(answers((SRC.parent / "ANSWERS.md").read_text()))
    leaks = hex_leaks(DST)
    if leaks:
        raise SystemExit(f"captured hex identifiers left in the fixtures: {leaks[:5]}")
    (DST / "ids.local").write_text("\n".join([*_ids, *_calls]))   # for the leak check; deleted by it
    print(counts, names)


if __name__ == "__main__":
    main()
