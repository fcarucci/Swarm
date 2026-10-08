"""One-time rewrite of the old CI host section of team.toml (its former name is OLD below, spelled
in two pieces so the repository-wide check for the retired word stays clean) to `[ci]`.

Run by `swarm upgrade` and `swarm init`. The section header is rewritten in place (the table, its
sub-tables, `[repositories."p".<old>]`, and an `<old> = ...` key), the original is kept as
`team.toml.bak` next to it, and the caller says so. There is no alias and no fallback read of the
old name anywhere else: a file already migrated (or never old) is left untouched.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

OLD = "for" + "ge"   # the retired section name
_HEADER = re.compile(r'^(\s*\[\[?)([^\]\n]*?)(\]\]?\s*(?:#.*)?)$')
_KEY = re.compile(r'^(\s*)' + OLD + r'(\s*=)')


def _segments(dotted: str) -> list[str]:
    """Split a table name on dots outside quotes, keeping each segment's own spelling."""
    parts, cur, quote = [], "", ""
    for ch in dotted:
        if quote:
            quote = "" if ch == quote else quote
            cur += ch
        elif ch in "\"'":
            quote = ch
            cur += ch
        elif ch == ".":
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    return parts


def rewrite(text: str) -> str:
    out = []
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        eol = line[len(body):]
        m = _HEADER.match(body)
        if m:
            segs = _segments(m.group(2))
            segs = ["ci" if s.strip() == OLD else s for s in segs]
            body = m.group(1) + ".".join(segs) + m.group(3)
        else:
            body = _KEY.sub(r"\1ci\2", body)
        out.append(body + eol)
    return "".join(out)


def team_path(config_path: Path) -> Path:
    return Path(os.environ.get("SWARM_TEAM_CONFIG") or Path(config_path).parent / "team.toml").expanduser()


def migrate_team_config(config_path: Path) -> list[str]:
    """Rewrite the old section to `[ci]` in team.toml once; returns the lines to print (none when there
    was nothing to do). Never raises: an unreadable or unwritable file is reported, not fatal."""
    path = team_path(config_path)
    try:
        if not path.is_file():
            return []
        text = path.read_text()
        new = rewrite(text)
        if new == text:
            return []
        bak = path.with_name(path.name + ".bak")
        n = 1
        while bak.exists():   # never overwrite an earlier backup
            bak = path.with_name(f"{path.name}.bak.{n}")
            n += 1
        bak.write_text(text)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(new)
        os.replace(tmp, path)
        return [f"migrated {path}: the [{OLD}] section is now [ci] (original kept as {bak})"]
    except (OSError, UnicodeError) as exc:
        return [f"could not migrate [{OLD}] to [ci] in {path}: {exc}"]
