"""The recorded Codex 0.157.1 session: payloads, rollouts, ids, ANSWERS.md."""
from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

FIX = Path(__file__).resolve().parent / "fixtures" / "codex" / "0.157.1"
sys.path.insert(0, str(FIX.parent))          # so tests can `from make_fixtures import SYNTH_B64`


def payloads(event: str) -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted((FIX / "hooks").glob(f"{event}-*.json"))]


def transcript_at_hook(event: str, n: int) -> Path | None:
    p = FIX / "hooks" / f"{event}-{n:02d}.transcript.jsonl"
    return p if p.exists() else None


def rollout(role: str) -> Path:
    return FIX / "rollouts" / f"{role}.jsonl"


def ids() -> dict:
    return json.loads((FIX / "ids.json").read_text())


def answer(q: str) -> dict[str, str]:
    """The fields of one ANSWERS.md line: "Qn <field>: <value>; <field>: <value> (evidence: ...)".
    Each value is the word after its field's ": " ("no (transcript_path ...)" -> "no"). A value's
    parenthesised evidence may itself contain ";", ": " and nested parentheses, so it is skipped
    as one balanced group. A line with no such fields (Q2's JSON) gives {}."""
    line = next(l for l in (FIX / "ANSWERS.md").read_text().splitlines() if l.startswith(q + " "))
    rest, out = line[len(q) + 1:], {}
    field = re.compile(r"([^;:()]+?): ([A-Za-z]+)")
    while rest:
        m = field.match(rest)
        if not m:
            break
        out[m.group(1).strip()] = m.group(2)
        rest = rest[m.end():]
        if rest.startswith(" ("):                  # skip the balanced (evidence ...) group
            depth, i = 0, 1
            for i, ch in enumerate(rest[1:], 1):
                depth += {"(": 1, ")": -1}.get(ch, 0)
                if depth == 0:
                    break
            rest = rest[i + 1:]
        if not rest.startswith("; "):
            break
        rest = rest[2:]
    return out


def staged(tmp: Path) -> Path:
    home = tmp / "codex-home"
    day = home / "sessions" / "2026" / "09" / "26"
    day.mkdir(parents=True, exist_ok=True)
    for role, name in json.loads((FIX / "rollouts" / "names.json").read_text()).items():
        shutil.copy(rollout(role), day / name)
    return home


def localize(payload: dict, codex_home: Path) -> dict:
    out = dict(payload)
    for key in ("transcript_path", "agent_transcript_path"):
        v = out.get(key)
        if isinstance(v, str):
            m = re.search(r"rollout-.*?-([0-9a-f-]{36})\.jsonl", v)
            if m:
                hit = next(iter(codex_home.glob(f"sessions/*/*/*/rollout-*-{m.group(1)}.jsonl")), None)
                if hit:
                    out[key] = str(hit)
    return out
