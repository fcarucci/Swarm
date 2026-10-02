"""Memory provenance: pin the memories swarm agents save to where they came from.

A memory write is a shell tool call of a known writer:
  swarm remember          (built in) prints `... [memory <doc_id> project "<project>"]`
  any other tool          named under [provenance] writers in the config: each entry is
                          {name, command, output[, bank]}: `command` a regex matched against the
                          shell command, `output` a regex matched against what it printed, with a
                          named group (?P<doc>...) for the document id (every match is one
                          document) and optionally (?P<bank>...) for the bank; `bank` is the
                          fallback bank (else the job's project bank)
The PostToolUse hook runs `hint` (one regex) on every shell command and nothing more unless it
matches; then `detect` reads the call's output, and record_from_hook stores a MemoryRef
with an excerpt of the agent's transcript.

Stdlib only at import time: the transcript code, the board and Hindsight are imported inside the
functions that need them.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
from dataclasses import dataclass, replace
from pathlib import Path
from swarm import compat

WRITE_HINT = re.compile(r"\bremember\b")
DOC_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@+=-]{0,199}")
BANK_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
MAX_DOCS = 20                  # document ids taken from one tool call at most
OUTPUT_SCAN = 65536            # characters of output scanned (the last ones)
COMMAND_SCAN = 1 << 20        # characters of a command scanned (the last ones)
BUILTIN_WRITER = "swarm-remember"
WRITER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,39}")   # board.base.WRITER_NAME: the same rule

_SWARM_CMD = re.compile(r"(?:^|[\s;&|(/'\"])swarm['\"]?\s+(?:--config\s+\S+\s+)?remember\b")
_SWARM_OUT = re.compile(r'\[memory (\S+) project "([^"\n]*)"\]\s*$', re.M)


@dataclass(frozen=True)
class Writer:
    """A memory-writing tool configured under [provenance] writers."""
    name: str
    command: "re.Pattern"
    output: "re.Pattern"
    bank: str = ""


def configured_writers(cfg: dict | None) -> tuple[Writer, ...]:
    """The [provenance] writers entries that are valid, compiled; an invalid one (no name, a name
    that is not WRITER_NAME or is the built-in's, a regex that does not compile, an output regex
    without a `doc` group, a fixed bank that is not a bank id) is skipped, and logged once per call."""
    raw = ((cfg or {}).get("provenance") or {}).get("writers")
    out = []
    for i, w in enumerate(raw if isinstance(raw, list) else []):
        try:
            name, bank = w["name"], w.get("bank") or ""
            if not isinstance(name, str) or WRITER_NAME.fullmatch(name) is None or name == BUILTIN_WRITER:
                raise ValueError(f"name {name!r} is not a writer name")
            command, output = re.compile(w["command"]), re.compile(w["output"], re.M)
            if "doc" not in output.groupindex:
                raise ValueError("output has no (?P<doc>...) group")
            if bank and (not isinstance(bank, str) or BANK_ID.fullmatch(bank) is None):
                raise ValueError(f"bank {bank!r} is not a bank id")
        except (KeyError, TypeError, AttributeError, ValueError, re.error) as exc:
            log(f"[provenance] writers[{i}] ignored: {type(exc).__name__}: {exc}")
            continue
        out.append(Writer(name, command, output, bank))
    return tuple(out)


@dataclass(frozen=True)
class MemoryWrite:
    writer: str
    bank: str                       # "" = the job's project bank (the hook resolves it)
    document_ids: tuple[str, ...]
    project: str | None = None      # swarm remember only


@dataclass(frozen=True)
class Detection:
    writes: tuple[MemoryWrite, ...]
    problems: tuple[str, ...]       # a write happened but can't be pinned (logged by the hook)


def hint(command: str | None, writers: tuple = ()) -> bool:
    """The cheap pre-check the hook runs on every shell command (`writers`: configured_writers)."""
    if not command:
        return False
    command = command[-COMMAND_SCAN:]
    return WRITE_HINT.search(command) is not None or any(w.command.search(command) for w in writers)


def valid_document_id(s) -> bool:
    return isinstance(s, str) and DOC_ID.fullmatch(s) is not None


def output_text(tool_response) -> str:
    """The text a success line is looked for in: Claude's Bash result {"stdout", "stderr", ...}
    gives its stdout only (a success line is never on stderr); Codex gives a string; a list of
    content items gives their text."""
    if isinstance(tool_response, str):
        return tool_response
    if isinstance(tool_response, dict):
        out = tool_response.get("stdout", tool_response.get("output"))
        return out if isinstance(out, str) else ""
    if isinstance(tool_response, list):
        return "\n".join(str(i.get("text", "")) for i in tool_response if isinstance(i, dict))
    return ""


def _ids(found) -> tuple[str, ...]:
    return tuple(dict.fromkeys(i for i in found if valid_document_id(i)))[:MAX_DOCS]


def _heredoc_ids(command: str) -> list[str]:
    out = []
    for raw in _JSON_DOC_ID.findall(command):
        try:
            out.append(json.loads(f'"{raw}"'))
        except ValueError:
            continue
    return out


def detect(command: str, output: str, writers: tuple = ()) -> Detection:
    from swarm.hindsight import bank_id
    command = (command or "")[-COMMAND_SCAN:]
    output = (output or "")[-OUTPUT_SCAN:]
    writes, problems = [], []
    if _SWARM_CMD.search(command):
        for doc, project in _SWARM_OUT.findall(output):
            if valid_document_id(doc):
                writes.append(MemoryWrite(BUILTIN_WRITER, bank_id(project) if project else "", (doc,), project=project))
    for w in writers:
        if not w.command.search(command):
            continue
        ids, banks = [], []
        for m in w.output.finditer(output):
            ids.append(m.group("doc"))
            banks.append(m.groupdict().get("bank") or w.bank)
        for bank in dict.fromkeys(banks):
            if bank and BANK_ID.fullmatch(bank) is None:
                problems.append(f"{w.name}: {bank!r} is not a bank id: not pinned")
                continue
            docs = _ids(d for d, b in zip(ids, banks) if b == bank)
            if docs:
                writes.append(MemoryWrite(w.name, bank, docs))
    total, capped = 0, []
    for w in writes:                       # MAX_DOCS over the whole call
        room = MAX_DOCS - total
        if room <= 0:
            break
        capped.append(w if len(w.document_ids) <= room else
                      MemoryWrite(w.writer, w.bank, w.document_ids[:room], w.project))
        total += len(capped[-1].document_ids)
    return Detection(tuple(capped), tuple(problems))


# --------------------------------------------------------------------------- the excerpt

DEFAULTS = {"enabled": True, "excerpt_turns": 20, "excerpt_max_kb": 256, "excerpt_image_mb": 5, "tail_mb": 8,
            "grace_days": 7, "check_days": 7, "check_max": 200}
EXCERPT_TYPE = "swarm-memory"
AFTER_LINES = 4          # lines already written after the call that the excerpt keeps
MAX_LINES = 200          # transcript lines in one excerpt at most
OUTPUT_CHARS = 4000      # characters of the call's output kept on the anchor line (after redaction)
MB = 1024 * 1024
EXCERPT_RESERVE = 0.5     # seconds of an excerpt's budget kept for the anchor-only fallback
EXCERPT_MAX_RAW = 4 * MB   # an excerpt's uncompressed size at most: = board.EXCERPT_MAX_RAW,
                           # which save_memory_ref enforces; pinned by test_raw_cap_matches_the_board
LINE_MAX = EXCERPT_MAX_RAW // 4   # one (redacted) transcript line larger than this becomes a marker
_CALL_TYPES = ("function_call", "custom_tool_call", "local_shell_call")


def settings(cfg: dict) -> dict:
    return {**DEFAULTS, **((cfg or {}).get("provenance") or {})}


def enabled(cfg: dict) -> bool:
    return bool(settings(cfg)["enabled"])


class UnsafeTranscript(Exception):
    """A transcript path that must not be read (the privfs rule): not a regular file of this
    user with one link, a symlink (at any component), or a compressed rollout."""


def read_tail(path, max_bytes: int) -> bytes:
    """The last max_bytes of a transcript, from its first whole line. Reached through safefs: every
    directory component is opened with O_NOFOLLOW and checked, the file itself with O_NOFOLLOW |
    O_NONBLOCK and checked on the descriptor (regular, this uid, one link), so nothing a sandbox
    planted (a symlink, a hard link to a private file, a FIFO) is read. Only the tail is read."""
    from swarm import safefs
    if str(path).endswith(".zst"):
        raise UnsafeTranscript(f"{path}: compressed rollout (not a live one): no excerpt")
    where = Path(os.fspath(path))
    try:
        with safefs.dir_fd(where.parent, create=False) as d:
            fd = safefs.open_existing(d, where.name, os.O_RDONLY)
    except (OSError, ValueError) as exc:   # ELOOP (a symlink), a hard link, ENOENT, EACCES, a bad path
        raise UnsafeTranscript(f"{path}: {getattr(exc, 'strerror', None) or exc}") from None
    try:
        size = os.fstat(fd).st_size
        start = max(0, size - max_bytes)
        os.lseek(fd, start, os.SEEK_SET)
        chunks, got = [], 0
        while got < max_bytes:      # never more than max_bytes, even if the file grows meanwhile
            b = os.read(fd, min(max_bytes - got, MB))
            if not b:
                break
            chunks.append(b)
            got += len(b)
    finally:
        os.close(fd)
    data = b"".join(chunks)
    if start:
        cut = data.find(b"\n")
        data = data[cut + 1:] if cut >= 0 else b""
    return data


def _entries(data: bytes) -> list[tuple[str, dict | None]]:
    out = []
    for raw in data.decode("utf-8", errors="replace").splitlines(keepends=True):
        try:
            e = json.loads(raw)
        except ValueError:
            e = None
        out.append((raw if raw.endswith("\n") else raw + "\n", e if isinstance(e, dict) else None))
    return out


def _ts(e) -> _dt.datetime | None:
    v = (e or {}).get("timestamp")
    try:
        t = _dt.datetime.fromisoformat(str(v).replace("Z", "+00:00")) if v else None
    except ValueError:
        return None
    return t.replace(tzinfo=_dt.timezone.utc) if t is not None and t.tzinfo is None else t


def _last_before(entries, at) -> int:
    """The last line written at or before `at` (a line with a timestamp); -1 if none."""
    for i in range(len(entries) - 1, -1, -1):
        t = _ts(entries[i][1])
        if t is not None and t <= at:
            return i
    return -1


def _claude_anchor(entries, call_id, at) -> int:
    for i in range(len(entries) - 1, -1, -1):
        e = entries[i][1]
        if e and e.get("type") == "assistant":
            content = (e.get("message") or {}).get("content")
            for b in content if isinstance(content, list) else []:
                if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id") == call_id:
                    return i
    return _last_before(entries, at)


def _codex_anchor(entries, call_id, at) -> int:
    fallback = None
    for i in range(len(entries) - 1, -1, -1):
        e = entries[i][1]
        p = (e or {}).get("payload") or {}
        if not e or e.get("type") != "response_item" or not isinstance(p, dict) or p.get("type") not in _CALL_TYPES:
            continue
        if call_id and p.get("call_id") == call_id:
            return i
        if fallback is None and (_ts(e) is None or _ts(e) <= at):
            fallback = i
    return fallback if fallback is not None else _last_before(entries, at)


def excerpt_lines(data: bytes, harness: str, call_id: str | None, at, turns: int) -> list[str]:
    """The raw transcript lines of the excerpt: back from the anchor until `turns` turns (as
    transcript_view counts them) or MAX_LINES, then up to AFTER_LINES lines already written after
    it, none later than `at`."""
    from swarm import transcript_view
    at = _aware(at)
    entries = _entries(data)
    if not entries:
        return []
    i = _codex_anchor(entries, call_id, at) if harness == "codex" else _claude_anchor(entries, call_id, at)
    if i < 0:                  # nothing written at or before the hook time: the anchor line alone
        return []
    picked, count = [], 0
    for raw, e in reversed(entries[:i + 1]):
        picked.append(raw)
        count += len(transcript_view.turns(raw)) if e is not None else 0
        if count >= turns or len(picked) >= MAX_LINES:
            break
    after = [raw for raw, e in entries[i + 1:i + 1 + AFTER_LINES] if _ts(e) is None or _ts(e) <= at]
    return list(reversed(picked)) + after


def _aware(at):
    return at.replace(tzinfo=_dt.timezone.utc) if isinstance(at, _dt.datetime) and at.tzinfo is None else at


_PEM_MARK = re.compile(r"-----(BEGIN|END) [A-Z0-9 ]*PRIVATE KEY-----")


def _tail_lines(text: str, limit: int) -> tuple[str, int]:
    """(the last `limit` characters of text from a line start, redactions). A cut never leaves a
    partial line (unless the kept part has no line break), and never the rest of a private key
    whose BEGIN line was cut off: everything up to such an orphaned END line is replaced."""
    if len(text) <= limit:
        return text, 0
    tail = text[-limit:]
    nl = tail.find("\n")
    if nl >= 0:
        tail = tail[nl + 1:]
    m = _PEM_MARK.search(tail)
    if m and m.group(1) == "END":
        return f"[… {len(text) - len(tail)} chars cut …]\n[REDACTED:private-key]" + tail[m.end():], 1
    return f"[… {len(text) - len(tail)} chars cut …]\n" + tail, 0


def _anchor(writes, harness: str, call_id: str | None, at, output: str, deadline=None) -> tuple[str, int]:
    """(the anchor line, redactions in the output). The output is redacted before it is cut to
    OUTPUT_CHARS, so a cut never splits a secret in a way redaction no longer recognises; only
    the last OUTPUT_SCAN characters are redacted at all (the detector's window), cut the same way."""
    from swarm import transcripts
    out, n = _tail_lines(output or "", OUTPUT_SCAN)
    out, m = transcripts.redact(out, deadline)
    out, k = _tail_lines(out, OUTPUT_CHARS)
    at = _aware(at)
    return json.dumps({"type": EXCERPT_TYPE, "timestamp": at.isoformat().replace("+00:00", "Z"),
                       "harness": harness, "tool_call_id": call_id,
                       "memories": [{"writer": w.writer, "bank": w.bank, "document_id": d}
                                    for w in writes for d in w.document_ids],
                       "output": out}, ensure_ascii=False) + "\n", n + m + k


def anchor_line(writes, harness: str, call_id: str | None, at, output: str) -> str:
    """The excerpt's last line: which memories the call saved, and its output (redacted, then cut)."""
    return _anchor(writes, harness, call_id, at, output)[0]


def _oversized(line: str) -> str:
    from swarm.transcripts import TRUNCATED_TYPE
    return json.dumps({"type": TRUNCATED_TYPE, "omitted_lines": 1, "omitted_bytes": len(line.encode("utf-8")),
                       "note": "one transcript line too large for a memory excerpt"}) + "\n"


@dataclass(frozen=True)
class Excerpt:
    body: bytes            # lzma of the redacted excerpt text (image placeholders in it)
    raw_bytes: int         # that text's size, uncompressed
    redactions: int
    turns: int
    images: tuple = ()     # TranscriptImage, only those whose placeholder is still in the text


def make_excerpt(path, harness, call_id, writes, output, at, deadline=None, s=None) -> Excerpt:
    """The excerpt of the agent's transcript at `path` for a memory write: excerpt_lines plus the
    anchor line, images out, redacted, a line over LINE_MAX replaced by a marker, then at most
    EXCERPT_MAX_RAW uncompressed and excerpt_max_kb compressed (oldest lines dropped first; the
    anchor always stays). Out of time (EXCERPT_RESERVE before the deadline), the anchor line
    alone. Raises UnsafeTranscript, OSError, transcripts.OutOfTime (not even the anchor fits)."""
    from swarm import transcript_view, transcripts
    s = s or settings({})
    data = read_tail(path, int(float(s["tail_mb"]) * MB))
    transcripts._check(deadline)
    anchor, n_out = _anchor(writes, harness, call_id, at, output, deadline)
    # The transcript lines get the budget less EXCERPT_RESERVE; out of time there, the excerpt
    # is the anchor line alone (the ref is still stored, with its output): OutOfTime only when
    # even that doesn't fit.
    soft = deadline - EXCERPT_RESERVE if deadline is not None else None
    try:
        lines = excerpt_lines(data, harness, call_id, at, int(s["excerpt_turns"]))
        transcripts._check(soft)
        return _excerpt(lines, anchor, n_out, s, soft)
    except transcripts.OutOfTime:
        return _excerpt([], anchor, n_out, s, deadline)


def _excerpt(lines: list[str], anchor: str, n_out: int, s: dict, deadline) -> Excerpt:
    from swarm import transcript_view, transcripts
    red, n, images = transcripts.prepare_text("".join(lines) + anchor, deadline)
    n += n_out
    budget, kept = int(float(s["excerpt_image_mb"]) * MB), []
    for img in images:                              # earliest first, while they fit
        if img.size <= budget:
            kept.append(img)
            budget -= img.size
    cap = int(float(s["excerpt_max_kb"]) * 1024)
    red_lines = red.splitlines(keepends=True)
    red_lines = [_oversized(x) if len(x) > LINE_MAX // 4 and len(x.encode("utf-8")) > LINE_MAX else x
                 for x in red_lines[:-1]] + red_lines[-1:]           # the anchor (last) is small and stays
    while True:
        data = "".join(red_lines).encode("utf-8")
        if len(data) <= EXCERPT_MAX_RAW or len(red_lines) <= 1:
            body = transcripts.compress(data, deadline)
            if len(body) <= cap or len(red_lines) <= 1:
                break
        red_lines = red_lines[max(1, len(red_lines) // 4):]   # drop the oldest quarter; the anchor stays last
    text = "".join(red_lines)
    shown = transcripts.image_refs(text)
    return Excerpt(body, len(text.encode("utf-8")), n, len(transcript_view.turns(text)),
                   tuple(i for i in kept if i.sha256 in shown))


# --------------------------------------------------------------------------- the hook
#
# Guards: document_id alone is the key, and the first agent to
# record it keeps it (Board.save_memory_ref); another agent's later claim is "kept": it changes
# nothing, on the board or in Hindsight, and is logged with the existing owner. agent_key is the
# hook's own (payload or enrolment), never anything the call printed. A `swarm remember` counts
# only into the agent's own job's project: the project in its output is data.

PATCH_SECONDS = 1.0     # the optional metadata patch's share of the hook budget, at most
# The ids `swarm remember` gives its documents (new_document_id; a spooled one: swarm-spool-<the
# record's uuid hex>). A swarm-remember claim on any other id is not pinned: such a row would say
# patched=True (its metadata written at write time), which only swarm remember's own ids earn.
SWARM_DOC_ID = re.compile(r"swarm-(?:spool-)?[0-9a-f]{32}")


def log(message: str) -> None:
    """One line in the hook error log (host-only dir; escaped to one printable line)."""
    from swarm.transcripts import log as _log
    _log(message)


def hook_metadata(job, name, agent_key, harness, host, session_id, call_id, at) -> dict[str, str]:
    """The provenance a metadata patch writes: swarm_-prefixed keys, so the writer's
    own source/host keys are never overwritten; empty values left out."""
    m = {"swarm_source": "swarm", "swarm_job": job, "swarm_agent": name, "swarm_agent_key": agent_key,
         "swarm_harness": harness, "swarm_host": host, "swarm_session_id": session_id or "",
         "swarm_tool_call_id": call_id or "", "swarm_captured_at": at.isoformat(timespec="seconds")}
    return {k: str(v) for k, v in m.items() if v}


def patch_metadata(cfg, bank, document_id, metadata, deadline, at=None) -> bool:
    """Patch the document's metadata, only when the cached capability says the server takes it
    (never a probe), within PATCH_SECONDS and the hook's deadline. `at`: the hook's time of the
    write; a document last written long before it is not patched (hindsight.PATCH_STALE_SECONDS).
    True if patched; a failure is logged."""
    why = _patch(cfg, bank, document_id, metadata, deadline, at)
    if why:
        log(f"memory provenance: metadata of {document_id} in {bank} not patched: {why}")
    return why is None


def _patch(cfg, bank, document_id, metadata, deadline, at=None) -> str | None:
    """patch_metadata without the log: None if patched, "" if the patch is off (no url, or the
    cache doesn't say the server takes it), else why it failed."""
    import time
    from swarm import hindsight
    if not hindsight.enabled(cfg) or not hindsight.metadata_patch_supported(cfg):
        return ""
    until = min(deadline, time.monotonic() + PATCH_SECONDS)
    if until - time.monotonic() < 0.1:
        return "out of time"
    client = hindsight.Client({**cfg, "hindsight": {**cfg["hindsight"], "deadline": until}})
    try:
        client.patch_document_metadata(bank, document_id, metadata, written_after=_aware(at))
        return None
    except (hindsight.HindsightUnavailable, hindsight.HindsightError, OSError, ValueError) as exc:
        return str(exc) or type(exc).__name__


def _owner(board, document_id: str):
    """The recorded ref of document_id, or None."""
    found = board.memory_refs(document_id=document_id)
    return found[0] if found else None


def _log_kept(board, document_id: str, agent_id: str, owner=None) -> None:
    owner = owner or _owner(board, document_id)
    who = f"{owner.agent_name} ({owner.agent_key}, job {owner.job})" if owner is not None else "?"
    log(f"memory provenance ({agent_id}): memory {document_id} is already linked to another agent's "
        f"transcript, {who}; this claim by {agent_id} was ignored")


def record_from_hook(board, cfg, host, agent_id, sid, payload, command, bound, deadline) -> list:
    """PostToolUse of a shell call whose command passed `hint`: record a MemoryRef for each
    document it wrote, if the caller is a member of one of this session's jobs (`bound`).
    Returns [(document_id, "inserted"|"updated"|"kept")]. The excerpt is best effort (a
    missing, unsafe or too-slow transcript leaves it out and says so in the log); the reference
    is always recorded. A claim on a document another agent recorded first is "kept": nothing
    is written for it (no excerpt, no row change, no metadata patch), and it is logged with its
    owner. Problems are logged, one line each."""
    from swarm import hindsight
    from swarm.board import MemoryRef
    from swarm.board.base import valid_name
    output = output_text(payload.get("tool_response"))
    det = detect(command, output, configured_writers(cfg))
    if not det.writes and not det.problems:
        return []
    if not isinstance(agent_id, str) or not agent_id:
        return []
    job = board.route(agent_id).member_job        # agent_id: the hook's, from payload/enrolment
    if not job or job not in bound:
        return []
    for problem in det.problems:
        log(f"memory provenance ({agent_id}): {problem}")
    project = hindsight.project_of(board.job_status(job), job)
    job_bank = hindsight.bank_id(project)
    writes = []
    for w in det.writes:
        if w.writer == BUILTIN_WRITER:          # its own job's project only; the output's is data
            if w.project and w.project != project:
                log(f"memory provenance ({agent_id}): swarm remember into project {w.project!r}, not the "
                    f"project of its job {job} ({project!r}): {', '.join(w.document_ids)} not pinned")
                continue
            bad = [d for d in w.document_ids if not SWARM_DOC_ID.fullmatch(d)]
            if bad:
                log(f"memory provenance ({agent_id}): {', '.join(bad)}: not a swarm remember document id "
                    f"(swarm-<32 hex> or swarm-spool-<32 hex>): not pinned")
            ids = tuple(d for d in w.document_ids if SWARM_DOC_ID.fullmatch(d))
            if not ids:
                continue
            w = MemoryWrite(w.writer, job_bank, ids, project)
        elif not w.bank:                          # a configured writer that names no bank: the job's
            w = MemoryWrite(w.writer, job_bank, w.document_ids, w.project)
        writes.append(w)
    out: list[tuple[str, str]] = []
    todo = []                                      # (write, document_id) this agent may record
    for w in writes:
        for doc in w.document_ids:
            owner = _owner(board, doc)
            if owner is not None and owner.agent_key != agent_id:
                _log_kept(board, doc, agent_id, owner)
                out.append((doc, "kept"))
            else:
                todo.append((w, doc))
    if not todo:
        return out
    state = board.sync_state(agent_id)
    name = state.name if state is not None and valid_name(state.name) else agent_id
    if not valid_name(name):
        log(f"memory provenance ({agent_id}): no valid agent name: {', '.join(d for _, d in todo)} not pinned")
        return out
    call_id = payload.get("tool_use_id") if isinstance(payload.get("tool_use_id"), str) else None
    at = _dt.datetime.now(_dt.timezone.utc)
    excerpt, why = None, None
    try:
        path = host.own_transcript(payload)
    except Exception as exc:
        path, why = None, f"{type(exc).__name__}: {exc}"
    if path is None:
        why = why or "the transcript path is missing or not one this host writes for this agent"
    else:
        mine = tuple(MemoryWrite(w.writer, w.bank, tuple(d for v, d in todo if v is w), w.project)
                     for w in writes if any(v is w for v, _ in todo))
        try:
            excerpt = make_excerpt(path, host.name, call_id, mine, output, at, deadline, settings(cfg))
        except Exception as exc:          # UnsafeTranscript, OSError, OutOfTime, bad data
            why = f"{type(exc).__name__}: {exc}"
    node, saved = compat.node(), []
    # patch_on: False once the patch is off (not applicable: nothing tried, nothing logged);
    # patch_error: after the first failed patch, no more tries this hook, the rest listed with it
    patch_on, patch_error, unpatched = True, None, []
    for w, doc in todo:
        ref = MemoryRef(document_id=doc, bank=w.bank, job=job, agent_key=agent_id, agent_name=name,
                        harness=host.name, host=node, session_id=sid, tool_call_id=call_id, writer=w.writer,
                        excerpt=excerpt.body if excerpt else None,
                        raw_bytes=excerpt.raw_bytes if excerpt else 0,
                        redactions=excerpt.redactions if excerpt else 0,
                        images=excerpt.images if excerpt else (),
                        patched=w.writer == BUILTIN_WRITER)   # it wrote its own metadata
        try:
            outcome = board.save_memory_ref(ref)
        except ValueError as exc:                 # refused by the board's checks: nothing stored
            log(f"memory provenance ({agent_id}): {doc} not recorded: {exc}")
            continue
        out.append((doc, outcome))
        if outcome == "kept":                     # another agent recorded it meanwhile
            _log_kept(board, doc, agent_id)
            continue
        saved.append(doc)
        if w.writer == "swarm-remember" or not patch_on:   # its own metadata / the patch is off
            continue
        if patch_error is not None:               # Hindsight failed (or hangs) already: not again
            unpatched.append(doc)
            continue
        meta = hook_metadata(job, name, agent_id, host.name, node, sid, call_id, at)
        err = _patch(cfg, w.bank, doc, meta, deadline, at)   # only once the row is this agent's
        if err == "":                             # the patch is off: not applicable, never logged
            patch_on = False
        elif err is not None:
            patch_error = err
            unpatched.append(doc)
        else:
            row = _owner(board, doc)
            if row is not None and row.agent_key == agent_id:
                board.save_memory_ref(replace(ref, patched=True, created_at=row.created_at))
    if unpatched:
        log(f"memory provenance ({agent_id}): metadata of {', '.join(unpatched)} not patched: {patch_error}")
    if why and saved:
        log(f"memory provenance ({agent_id}): {', '.join(saved)} recorded without an excerpt: {why}")
    return out


# ---- `swarm remember` names its document and carries its provenance

META_KEYS_MAX = 16      # a memory's provenance metadata: at most this many keys,
META_KEY_MAX = 40       # each key at most this long ([A-Za-z0-9_], not starting with swarm_,
META_VALUE_MAX = 300    # which a metadata patch owns), each value a string at most this long
_META_KEY = re.compile(r"[A-Za-z0-9_]+")
_TAG_UNSAFE = re.compile(r'["\[\]]')   # would end the tag's project early or open a second tag


def metadata_ok(key, value) -> bool:
    """One key/value of `swarm remember`'s metadata: a plain key (not swarm_*, the patch's
    prefix) and a short string value without control characters."""
    from swarm.textsafe import has_controls
    return (isinstance(key, str) and 0 < len(key) <= META_KEY_MAX and _META_KEY.fullmatch(key) is not None
            and not key.lower().startswith("swarm_")
            and isinstance(value, str) and len(value) <= META_VALUE_MAX and not has_controls(value))


def valid_metadata(m) -> bool:
    """A memory's metadata as the spool accepts it (sandbox-writable data): a small dict of
    metadata_ok items. cli_metadata only ever produces such a dict."""
    return isinstance(m, dict) and len(m) <= META_KEYS_MAX and all(metadata_ok(k, v) for k, v in m.items())


def new_document_id() -> str:
    """The document id `swarm remember` picks for a memory it stores directly."""
    import uuid
    return "swarm-" + uuid.uuid4().hex


def output_tag(document_id: str, project: str | None) -> str:
    """What `swarm remember` appends to every line that stored or queued a memory; detect()
    reads it back from the tool call's output. ValueError for a document id or project that
    could forge a second tag (quotes, brackets, control characters): cmd_remember checks the
    project before storing anything, this is the second line of defence."""
    from swarm.textsafe import has_controls
    project = project or ""
    if not valid_document_id(document_id):
        raise ValueError(f"not a document id: {document_id!r}")
    if _TAG_UNSAFE.search(project) or has_controls(project) or "\n" in project:
        raise ValueError(f"project {project!r} can't be shown in a memory tag")
    return f'[memory {document_id} project "{project}"]'


def cli_metadata(env) -> dict[str, str]:
    """What `swarm remember` knows about where it runs: no tool-call id, which only
    the hook has. Empty values, and any the spool would refuse (metadata_ok), are dropped."""
    from swarm import hosts
    m = {"harness": hosts.detect_cli_host(env) or "", "host": compat.node(),
         "session_id": hosts.cli_session_id(env) or "",
         "captured_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")}
    return {k: v for k, v in m.items() if v and metadata_ok(k, v)}


# --------------------------------------------------------------------------- reading

def printable(text) -> str:
    """Text safe for a terminal: rows and excerpts may be forged (a sandboxed agent with write
    access to a SQLite/file board), so every control character of theirs becomes visible
    notation (textsafe.term_safe); newlines and tabs are kept."""
    from swarm.textsafe import term_safe
    return term_safe(text, keep_newlines=True)


STATUS_SECONDS = 3.0
STALE_CLAIM = 600     # seconds: a document last written this long before the reference is suspicious
LATE_CLAIM = 600      # seconds: so is one first or last written this long after it (a claim on a
                      # predictable id before its document existed, or a document rewritten since)


def status(cfg: dict, ref, client=None) -> str:
    """Whether the ref's document is still in Hindsight, in words (one GET, at most
    STATUS_SECONDS): "not checked ([hindsight] url is empty)", "present (last written <ts>)",
    "missing" (Hindsight's own 404, Client.document) or "unknown (<why>)". A present document
    whose times don't fit the reference is a doubtful claim:
    "present, but first written <ts>, after this reference: ..." (the id was claimed before the
    document existed: squatting on a predictable id), "present, but last written <ts>, after
    this reference: ..." (rewritten since, maybe by someone else) or "present, but last written
    <ts>, before this reference: ..." (a forged success line naming an old document), each
    ending "the claim may be wrong"."""
    import time
    from swarm import hindsight
    if not hindsight.enabled(cfg):
        return "not checked ([hindsight] url is empty)"
    if not valid_document_id(ref.document_id) or not isinstance(ref.bank, str) or not BANK_ID.fullmatch(ref.bank):
        return "unknown (not a valid document id or bank: not asked)"   # a forged row: no odd URL path
    client = client or hindsight.Client({**cfg, "hindsight": {**cfg["hindsight"],
                                                              "deadline": time.monotonic() + STATUS_SECONDS}})
    try:
        doc = client.document(ref.bank, ref.document_id)
    except (hindsight.HindsightUnavailable, hindsight.HindsightError) as exc:
        return f"unknown ({exc})"
    if doc is None:
        return "missing"
    if not isinstance(doc, dict):
        return "unknown (not a document in Hindsight's answer)"
    written = str(doc.get("updated_at") or doc.get("created_at") or "")
    first = str(doc.get("created_at") or "")
    when, born = _time(written), _time(first)
    if when is None:
        return f"present (last written {written or '?'})"
    created = ref.created_at
    if created is not None and created.tzinfo is None:
        created = created.replace(tzinfo=_dt.timezone.utc)
    if created and born is not None and (born - created).total_seconds() > LATE_CLAIM:
        return f"present, but first written {first}, after this reference: the claim may be wrong"
    if created and (when - created).total_seconds() > LATE_CLAIM:
        return f"present, but last written {written}, after this reference: the claim may be wrong"
    if created and (created - when).total_seconds() > STALE_CLAIM:
        return f"present, but last written {written}, before this reference: the claim may be wrong"
    return f"present (last written {written})"


def _time(value: str) -> _dt.datetime | None:
    """An ISO 8601 time from Hindsight (naive: UTC), else None."""
    try:
        t = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo is not None else t.replace(tzinfo=_dt.timezone.utc)


# --------------------------------------------------------------------------- retention

PRUNE_SECONDS = 60.0


@dataclass(frozen=True)
class PruneResult:
    """What one prune did. `checked`: refs Hindsight answered for (the document there, or gone,
    or its bank missing). `dropped`: the document ids of the refs deleted (`dropped_refs`:
    those refs, as read before the delete).
    `unknown`: refs asked about, or looked at, without a clear answer and kept (an error, a 404
    that isn't Hindsight's, a reply that isn't a document, the question the outage cut short, a
    forged row never asked). `skipped`: due refs left for a later run (check_max, the deadline,
    or Hindsight unreachable). `note`: why nothing was checked at all. `kept_missing_bank`: of
    `checked`, the refs whose bank Hindsight doesn't have (kept, and marked checked)."""
    checked: int
    dropped: tuple = ()
    unknown: int = 0
    note: str | None = None
    skipped: int = 0
    kept_missing_bank: int = 0
    dropped_refs: tuple = ()


def _setting(s: dict, key: str) -> float:
    """A non-negative number from [provenance], else its default."""
    try:
        v = float(s[key])
    except (TypeError, ValueError):
        return float(DEFAULTS[key])
    return v if v >= 0 and v == v and v != float("inf") else float(DEFAULTS[key])


def _askable(ref) -> bool:
    """Whether a row's ids may go into a URL: rows can be forged (a sandboxed agent with write
    access to a SQLite/file board), and such a row is never asked about, so never dropped."""
    return valid_document_id(ref.document_id) and isinstance(ref.bank, str) and bool(BANK_ID.fullmatch(ref.bank))


def prune(board, cfg: dict, client=None, deadline: float | None = None) -> PruneResult:
    """Drop the refs whose memory Hindsight no longer has, lazily: only refs older
    than grace_days and not checked within check_days, oldest check first, at most check_max
    GETs per call (bank profiles included), all by `deadline` (the client's own deadline too).

    A ref goes only on proof: its bank exists (GET profile, once per bank per call) and
    Hindsight's own 404 for its document (Client.document). The delete is conditional: a row
    re-recorded or checked again since it was read stays (Board.delete_memory_refs expected).
    Refs of a missing bank are kept and marked checked (a wrong or fresh server is as likely as
    a deleted bank; marked, they don't come first again on every run). Hindsight unreachable:
    nothing more is asked. Any other answer (an error, a foreign 404, something that isn't a
    document) keeps the ref. Never raises for Hindsight."""
    import time
    from swarm import hindsight
    if not hindsight.enabled(cfg):
        return PruneResult(0, (), 0, "memory references not checked: [hindsight] url is empty")
    s = settings(cfg)
    now = board.now()
    grace = _dt.timedelta(days=_setting(s, "grace_days"))
    every = _dt.timedelta(days=_setting(s, "check_days"))
    limit = int(_setting(s, "check_max"))

    def aware(t):
        return t.replace(tzinfo=_dt.timezone.utc) if t is not None and t.tzinfo is None else t

    due = [r for r in board.memory_refs()
           if r.created_at is not None and aware(r.created_at) <= now - grace
           and (r.checked_at is None or aware(r.checked_at) <= now - every)]
    due.sort(key=lambda r: (aware(r.checked_at or r.created_at), r.document_id))
    if client is None:
        h = dict(cfg["hindsight"])
        if deadline is not None:
            h["deadline"] = deadline
        client = hindsight.Client({**cfg, "hindsight": h})
    present, no_bank, gone, unknown, asked = [], [], [], 0, 0
    banks: dict[str, bool | None] = {}   # bank -> exists; None: no clear answer this run

    def may_ask() -> bool:
        return asked < limit and (deadline is None or time.monotonic() <= deadline)

    i = 0
    for i, r in enumerate(due):
        if not _askable(r):
            unknown += 1
            continue
        try:
            if r.bank not in banks:
                if not may_ask():
                    break
                asked += 1
                try:
                    banks[r.bank] = client.bank_exists(r.bank)
                except hindsight.HindsightUnavailable:
                    raise
                except Exception:
                    banks[r.bank] = None
            if banks[r.bank] is None:
                unknown += 1
                continue
            if not banks[r.bank]:
                no_bank.append(r)
                continue
            if not may_ask():
                break
            asked += 1
            doc = client.document(r.bank, r.document_id)
        except hindsight.HindsightUnavailable:
            unknown += 1
            i += 1
            break
        except Exception:   # an error answer, a foreign 404, a reply that isn't JSON
            unknown += 1
            continue
        if doc is None:
            gone.append(r)
        elif isinstance(doc, dict):
            present.append(r.document_id)
        else:
            unknown += 1
    else:
        i = len(due)
    skipped = len(due) - i
    if present or no_bank:
        board.mark_memory_refs_checked(present + [r.document_id for r in no_bank])
    dropped: tuple = ()   # MemoryRefs here; ids in the result
    if gone:
        expected = {r.document_id: (r.created_at, r.checked_at) for r in gone}
        if board.delete_memory_refs(list(expected), expected=expected) == len(gone):
            dropped = tuple(gone)
        else:   # some were re-recorded or re-checked meanwhile: report only what went
            left = {r.document_id for r in board.memory_refs() if r.document_id in expected}
            dropped = tuple(r for r in gone if r.document_id not in left)
    return PruneResult(len(present) + len(gone) + len(no_bank), tuple(r.document_id for r in dropped), unknown,
                       None, skipped, len(no_bank), dropped)


def statuses(cfg: dict, refs, deadline: float | None = None) -> dict[str, str]:
    """status() of each ref, by document id, one client for all (so an outage costs one
    timeout); refs not reached by `deadline` are "unknown (out of time)"."""
    import time
    from swarm import hindsight
    client = None
    if hindsight.enabled(cfg):
        h = dict(cfg["hindsight"])
        if deadline is not None:
            h["deadline"] = deadline
        client = hindsight.Client({**cfg, "hindsight": h})
    out = {}
    for r in refs:
        if deadline is not None and time.monotonic() > deadline and client is not None:
            out[r.document_id] = "unknown (out of time)"
            continue
        out[r.document_id] = status(cfg, r, client)
    return out
