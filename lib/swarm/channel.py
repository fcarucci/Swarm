"""Release channels: which revision of the swarm repository an install or upgrade follows.

  release  the newest vX.Y.Z tag (the default)
  main     the tip of the default branch

The marketplace is added pinned to the chosen ref (Claude: `<url>#<ref>`, Codex: `--ref <ref>`).
Tags are found with `git ls-remote --tags --refs --sort=-v:refname <url> 'v*'`, which works
anonymously on GitHub and Gitea. The channel an upgrade follows is kept in the swarm config
(`[upgrade] channel = "release" | "main"`) so a plain `swarm upgrade` keeps following it."""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

CHANNELS = ("release", "main")
DEFAULT_CHANNEL = "release"
_TAG = re.compile(r"^v\d+\.\d+\.\d+$")


def latest_tag(url: str, timeout: int = 30) -> str | None:
    """The newest vX.Y.Z tag at `url` (a git URL or a local repository path), or None when there
    is none or git ls-remote fails (no git, no network, a bad URL)."""
    try:
        res = subprocess.run(["git", "ls-remote", "--tags", "--refs", "--sort=-v:refname", url, "v*"],
                             capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
                             env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    for line in res.stdout.splitlines():
        ref = line.split("\t")[-1].strip()
        name = ref[len("refs/tags/"):] if ref.startswith("refs/tags/") else ref
        if _TAG.match(name):
            return name
    return None


def tip_commit(url: str, timeout: int = 30) -> str | None:
    """The commit at the tip of main at `url` (a git URL or local repository path), or None when
    git ls-remote fails or knows no such branch."""
    try:
        res = subprocess.run(["git", "ls-remote", url, "refs/heads/main"],
                             capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
                             env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    for line in res.stdout.splitlines():
        sha = line.split("\t")[0].strip()
        if re.fullmatch(r"[0-9a-f]{40,64}", sha):
            return sha
    return None


def resolve(channel: str, url: str, ref: str | None = None) -> tuple[str, str | None, str | None]:
    """(channel, ref, warning): the ref to pin the marketplace at (None = the default branch).
    An explicit `ref` wins. A release channel with no resolvable tag falls back to main, with a
    warning."""
    if ref:
        return channel, ref, None
    if channel == "main":
        return "main", None, None
    tag = latest_tag(url)
    if tag:
        return "release", tag, None
    return "main", None, (f"no release tag (vX.Y.Z) found at {url} (or git ls-remote failed): "
                          f"falling back to the tip of main")


_SECTION = re.compile(r"^\s*\[([^\]]*)\]\s*(#.*)?$")
_KEY = re.compile(r'^\s*channel\s*=')


def read_channel(config: Path | None = None) -> str | None:
    """`[upgrade] channel` from the swarm config, or None when unset or unreadable."""
    try:
        import tomllib
        from swarm import paths
        data = tomllib.loads((config or paths.config_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError, ImportError):
        return None
    v = (data.get("upgrade") or {}).get("channel") if isinstance(data.get("upgrade"), dict) else None
    return v if v in CHANNELS else None


def write_channel(channel: str, config: Path | None = None) -> bool:
    """Record `[upgrade] channel = "<channel>"` in an existing config file, leaving every other
    line alone. False when there is no config file to edit (a fresh one is made by bootstrap)."""
    if channel not in CHANNELS:
        raise ValueError(channel)
    from swarm import paths
    path = config or paths.config_path()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    new = f'channel = "{channel}"'
    start = next((i for i, ln in enumerate(lines) if (m := _SECTION.match(ln)) and m.group(1).strip() == "upgrade"), None)
    if start is None:
        lines += (["" ] if lines and lines[-1].strip() else []) + ["[upgrade]", new]
    else:
        end = next((i for i in range(start + 1, len(lines)) if _SECTION.match(lines[i])), len(lines))
        for i in range(start + 1, end):
            if _KEY.match(lines[i]):
                lines[i] = new
                break
        else:
            lines.insert(start + 1, new)
    tmp = path.with_name(path.name + ".swarm-tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    try:
        tmp.chmod(path.stat().st_mode & 0o777)
    except OSError:
        pass
    os.replace(tmp, path)
    return True
