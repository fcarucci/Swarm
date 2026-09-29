"""[supervise] settings and the supervisor's own files. Everything here is
stdlib and never touches the board.

The supervisor's files (run files, run locks, the pass lock and state, the outage window, the
transcript retries, the log) live in a private directory, ~/.local/share/swarm/supervisor (0700),
not under ~/.local/state/swarm: that one is a Codex writable root (codex_config.required), so a
sandboxed agent could plant files there that the supervisor, running outside the sandbox, would
act on. ensure_private_dir creates and checks it: a real directory (never a
symlink), this user's, mode 0700, under no Codex writable root of this config. Files left in the
old location are never read. The kill-switch file is honoured in both places: the documented
~/.local/state/swarm/supervise.off, and supervise.off in the private directory, which a
sandboxed agent can't delete."""
from __future__ import annotations

import datetime as _dt
import json
import math
import os
import re
import time
from pathlib import Path

from swarm import paths

DEFAULTS = {
    "enabled": False,                  # kill switch (off by default); also gates closing
    "silent_minutes": 90,              # no post and no tool call for this long -> stuck:silent
                                       # (90, above tool_timeout 60; 45 was too short in practice)
    "max_restarts_per_agent": 2,
    "max_restarts_per_job": 6,
    "backoff_minutes": [2, 10, 30],    # wait before attempt 1, 2, 3+ (last value repeats)
    "max_concurrent_replacements": 2,  # per host (every OS user's open restart rows)
    "max_turns": 60,                   # claude -p --max-turns
    "max_minutes": 60,                 # wall clock per replacement (both hosts)
    "max_restart_minutes": 180,        # per job, summed over its replacements
    "daily_restart_minutes": 480,      # per host (every OS user) per calendar day, runs overlapping it
    "min_minutes": 5,                  # don't start a replacement with less time than this left
    "max_budget_usd": 0,               # claude -p --max-budget-usd (0 = not passed)
    "codex_token_limit": 0,            # codex features.rollout_budget.limit_tokens (0 = off)
    "model_override": "",              # replacements run on this model on this host ("" = role model)
    "claude_permission_mode": "auto",  # claude -p --permission-mode (never the bypass mode)
    "enrol_minutes": 5,                # a replacement not on the board by then is killed
    "brief_posts": 20,                 # the agent's own recent posts in the brief
    "brief_turns": 40,                 # transcript tail in the brief (spec: ~40 turns)
    "brief_max_chars": 24000,          # the whole brief, tail trimmed first (>= MIN_BRIEF_CHARS)
    "claude_bin": "claude",
    "codex_bin": "codex",
    "claude_plugin_dir": "",           # e2e only: claude -p --plugin-dir <dir> (settings: user only)
    "run_output_days": 7,              # a replacement's redacted output tail is kept this long
    "allowed_workdirs": ["~/src"],     # a replacement's work dir must be under one of these
    "pass_env": [],                    # extra environment variables a replacement gets (an allowlist)
    "timer_minutes": 2,                # the systemd timer's period (spec: every 2 minutes)
}
_INTS = ("silent_minutes", "max_restarts_per_agent", "max_restarts_per_job",
         "max_concurrent_replacements", "max_turns", "max_minutes", "max_restart_minutes",
         "daily_restart_minutes", "min_minutes", "codex_token_limit", "enrol_minutes",
         "brief_posts", "brief_turns", "brief_max_chars", "timer_minutes", "run_output_days")
FORBIDDEN_PERMISSION_MODES = ("bypassPermissions",)
# the smallest brief_max_chars: room for the brief's header and transcript-reading commands, which
# are never trimmed, plus a little of the task and posts
MIN_BRIEF_CHARS = 2000


_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")


class SettingsError(ValueError):
    """[supervise] holds a value the supervisor refuses to run with (the message says which)."""


def settings(cfg: dict) -> dict:
    section = (cfg or {}).get("supervise")
    if section is None:
        section = {}
    if not isinstance(section, dict):   # `supervise = 2` instead of a [supervise] table
        raise SettingsError(f"[supervise] must be a table of settings, not {section!r}")
    s = {**DEFAULTS, **section}
    out = dict(s)
    if not isinstance(s["enabled"], bool):   # "false" (a string) must not switch it on
        raise SettingsError(f"[supervise] enabled must be true or false, not {s['enabled']!r}")
    out["enabled"] = s["enabled"]
    for k in _INTS:
        try:
            f = float(s[k])
        except (TypeError, ValueError, OverflowError):
            raise SettingsError(f"[supervise] {k} must be a whole number, not {s[k]!r}") from None
        if not math.isfinite(f):
            raise SettingsError(f"[supervise] {k} must be a whole number, not {s[k]!r}")
        try:
            out[k] = int(s[k])
        except (TypeError, ValueError, OverflowError):
            raise SettingsError(f"[supervise] {k} must be a whole number, not {s[k]!r}") from None
        if out[k] < 0:
            raise SettingsError(f"[supervise] {k} must not be negative")
    if out["codex_token_limit"] == 1:   # codex needs 0 < reminder_at_remaining_tokens < limit_tokens
        raise SettingsError("[supervise] codex_token_limit must be 0 (off) or at least 2")
    if out["brief_max_chars"] < MIN_BRIEF_CHARS:
        raise SettingsError(f"[supervise] brief_max_chars must be at least {MIN_BRIEF_CHARS}")
    try:
        out["max_budget_usd"] = float(s["max_budget_usd"])
        out["backoff_minutes"] = [float(x) if float(x) != int(float(x)) else int(float(x))
                                  for x in s["backoff_minutes"]]
    except (TypeError, ValueError, OverflowError):
        raise SettingsError("[supervise] backoff_minutes must be a list of minutes, "
                            "max_budget_usd a number") from None
    if not out["backoff_minutes"] or any(x < 0 for x in out["backoff_minutes"]):
        raise SettingsError("[supervise] backoff_minutes needs at least one value, none negative")
    if any(not math.isfinite(x) for x in out["backoff_minutes"]):
        raise SettingsError("[supervise] backoff_minutes must be finite numbers")
    if out["max_budget_usd"] < 0:
        raise SettingsError("[supervise] max_budget_usd must not be negative")
    if not math.isfinite(out["max_budget_usd"]):
        raise SettingsError("[supervise] max_budget_usd must be a finite number")
    mode = str(s["claude_permission_mode"])
    if mode in FORBIDDEN_PERMISSION_MODES:
        raise SettingsError(f"[supervise] claude_permission_mode = {mode!r} is refused: replacements "
                            f"never bypass permissions")
    out["claude_permission_mode"] = mode
    wd = s["allowed_workdirs"]
    if not isinstance(wd, list) or not all(isinstance(x, str) and x for x in wd):
        raise SettingsError("[supervise] allowed_workdirs must be a list of directories")
    out["allowed_workdirs"] = list(wd)
    pe = s["pass_env"]
    if not isinstance(pe, list) or not all(isinstance(x, str) and _ENV_NAME.fullmatch(x) for x in pe):
        raise SettingsError("[supervise] pass_env must be a list of environment variable names")
    out["pass_env"] = list(pe)
    for k in ("model_override", "claude_bin", "codex_bin", "claude_plugin_dir"):
        out[k] = str(s[k] or "")
    return out


class PrivateDirError(RuntimeError):
    """The supervisor's private directory is unsafe to use (the message says why and how to fix)."""


def private_dir() -> Path:
    return paths.share_dir() / "supervisor"


def off_file() -> Path:
    return paths.state_dir() / "supervise.off"


def off_files() -> list[Path]:
    """Every kill-switch file: the documented one and the private one (see the module docstring)."""
    return [off_file(), private_dir() / "supervise.off"]


def switched_off() -> Path | None:
    """The kill-switch file that exists, or None."""
    return next((p for p in off_files() if os.path.lexists(p)), None)


def log_path() -> Path:
    return private_dir() / "supervise.log"


def state_path() -> Path:
    return private_dir() / "supervise-state.json"


def runs_dir() -> Path:
    return private_dir() / "replacements"


def _writable_roots(cfg: dict | None) -> list[Path]:
    """The directories a Codex sandbox of this config may write that the swarm adds (state dir,
    spool, markers), resolved."""
    if not cfg:
        return [Path(os.path.realpath(paths.state_dir()))]
    from swarm import codex_config
    roots = codex_config.required(cfg)[("sandbox_workspace_write", "writable_roots")]
    return [Path(os.path.realpath(r)) for r in roots]


def _overlaps(root: Path) -> bool:
    """Whether writable root `root` (resolved) covers ~/.local/share/swarm or lies inside it."""
    tree = Path(os.path.realpath(paths.share_dir()))
    root = Path(os.path.realpath(root))
    return root == tree or root in tree.parents or tree in root.parents


def ensure_private_dir(cfg: dict | None = None, create: bool = True) -> Path:
    """The private directory (and its runs directory), created 0700 if missing and checked
    through descriptors (privfs.open_dir): PrivateDirError if anything from the home directory
    down is a symlink or not a directory, or another user's, or either directory isn't mode 0700,
    or it lies under a Codex writable root the swarm itself adds for `cfg` (state dir, spool,
    markers). The whole of ~/.local/share/swarm (venv, host/,
    supervisor/) is checked, and a root inside it counts as much as one above it.
    create=False (dry run): creates nothing, checks what exists. A writable root the user
    configured in Codex is codex_exposure's: the pass refuses to run (command._refuse_exposure)."""
    from swarm.supervisor import privfs
    d = private_dir()
    for root in _writable_roots(cfg):
        if _overlaps(root):
            raise PrivateDirError(f"{d} (or {paths.share_dir()}, which holds it) overlaps {root}, a "
                                  f"Codex writable root (the state directory, [board] spool_dir or "
                                  f"[hook] marker_dir): refusing to keep the supervisor's state "
                                  f"there; move that setting")
    for sub in (None, privfs.RUNS):
        try:
            os.close(privfs.open_dir(sub, create))
        except FileNotFoundError:
            if create:
                raise
            break
    return d


def _codex_roots(data: dict) -> list[str]:
    tables = [data.get("sandbox_workspace_write")]
    profiles = data.get("profiles")
    if isinstance(profiles, dict):
        tables += [p.get("sandbox_workspace_write") for p in profiles.values() if isinstance(p, dict)]
    out = []
    for t in tables:
        roots = t.get("writable_roots") if isinstance(t, dict) else None
        if isinstance(roots, list):
            out += [r for r in roots if isinstance(r, str)]
    return out


def codex_exposure() -> list[Path]:
    """The writable roots of the user's Codex config ($CODEX_HOME/config.toml, default
    ~/.codex/config.toml; read, never written), top level and profiles, that cover or lie inside
    ~/.local/share/swarm (the venv, host/ and the private directory): a sandboxed Codex agent
    may write it. [] when none (or no readable config). A Codex session started with its working
    root at $HOME (or above) exposes it too; that can't be read from a file, so doctor says so."""
    try:
        import tomllib
    except ImportError:   # pragma: no cover - Python < 3.11
        return []
    from swarm.hosts.codex import codex_home
    try:
        data = tomllib.loads((codex_home() / "config.toml").read_text())
    except (OSError, ValueError):
        return []
    return [Path(r) for r in _codex_roots(data) if _overlaps(Path(os.path.expanduser(r)))]


def off_reason(cfg: dict) -> str | None:
    """Why the local kill switches stop the supervisor now ([supervise] enabled, the off files),
    or None. Read fresh every call: checked again right before a launch and an enrolment."""
    try:
        if not settings(cfg)["enabled"]:
            return "[supervise] enabled = false"
    except SettingsError:
        return "[supervise] is invalid"
    off = switched_off()
    return f"{off} exists" if off is not None else None


def enabled(cfg: dict) -> bool:
    try:
        return settings(cfg)["enabled"] and switched_off() is None
    except SettingsError:
        return False


def log(message: str) -> None:
    try:
        from swarm.supervisor import privfs
        with privfs.dir_fd() as d:
            privfs.append(d, log_path().name, f"{time.strftime('%F %T')} {' '.join(str(message).split())}\n")
    except Exception:
        pass


def read_private(name: str) -> bytes | None:
    """A file of the private directory, read safely (privfs.read); None if missing or refused."""
    from swarm.supervisor import privfs
    try:
        with privfs.dir_fd(create=False) as d:
            return privfs.read(d, name)
    except (OSError, PrivateDirError):
        return None


def write_private(name: str, text: str) -> None:
    """Replace a file of the private directory safely (privfs.write_atomic)."""
    from swarm.supervisor import privfs
    with privfs.dir_fd() as d:
        privfs.write_atomic(d, name, text)


def load_state() -> dict:
    try:
        data = json.loads(read_private(state_path().name) or b"{}")
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def save_state(state: dict) -> None:
    write_private(state_path().name, json.dumps(state, sort_keys=True))


def today_start() -> _dt.datetime:
    """Local midnight today, tz-aware: the day of [supervise] daily_restart_minutes."""
    return _dt.datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
