"""How a replacement is started: `claude -p` with --max-turns and the
permission mode from [supervise] (never a bypass); `codex exec --sandbox workspace-write` with
the plugin's trusted hooks (never a bypass), optionally with the experimental rollout token
budget. Both get a wall clock from the runner. check_safe refuses any bypass flag.

Settings: a Claude replacement loads user settings only
(`--setting-sources user`, always, also with the e2e plugin dir): its work dir may hold a
.claude/settings.json planted by a sandboxed agent. check_safe refuses project or local setting
sources, always."""
from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass

FORBIDDEN = ("--dangerously-skip-permissions", "--allow-dangerously-skip-permissions",
             "--dangerously-bypass-approvals-and-sandbox", "--yolo",
             "--dangerously-bypass-hook-trust", "bypassPermissions", "danger-full-access")


class UnsafeLaunch(ValueError):
    """A replacement command carrying a bypass flag: never run."""


@dataclass(frozen=True)
class LaunchSpec:
    harness: str
    argv: tuple[str, ...]
    cwd: str
    stdin: str
    session_id: str | None
    minutes: float


def check_safe(argv) -> None:
    """Raise UnsafeLaunch when any argument carries a bypass flag or value (in any case, also
    inside `--flag=value` or a `-c key=value` override)."""
    for arg in argv:
        for bad in FORBIDDEN:
            if bad.lower() in str(arg).lower():
                raise UnsafeLaunch(f"refusing to start a replacement with {bad}")
    _check_setting_sources([str(a) for a in argv])


def _check_setting_sources(argv: list[str]) -> None:
    """Refuse `--setting-sources` naming project or local settings (or with no value). No
    exemption (the e2e harness never needed one)."""
    for i, arg in enumerate(argv):
        low = arg.lower()
        if low == "--setting-sources":
            value = argv[i + 1].lower() if i + 1 < len(argv) else None
        elif low.startswith("--setting-sources="):
            value = low.split("=", 1)[1]
        else:
            continue
        if value is None or not value.strip():
            raise UnsafeLaunch("refusing to start a replacement with --setting-sources and no value")
        if "project" in value or "local" in value:
            raise UnsafeLaunch(f"refusing to start a replacement with --setting-sources {value}: "
                               f"replacements load user settings only")


_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@\[\]-]{0,127}$")


def replacement_model(cfg: dict, harness: str, role: str | None, recorded: str | None) -> str | None:
    """[supervise] model_override, else the role's [models] entry, else the model the agent row
    recorded. The row is board data (writable by agents): a recorded value that isn't a plain
    model name (one starting with "-" would be read as a flag) is ignored."""
    from swarm import models, roles
    from swarm.supervisor.settings import settings
    override = settings(cfg)["model_override"]
    if override:
        return override
    if recorded is not None and not _MODEL.match(str(recorded)):
        recorded = None
    return models.model_for(cfg, harness, role if roles.valid_name(role) else "worker") or recorded


def claude_spec(cfg: dict, *, prompt: str, workdir: str, model: str | None, minutes: float,
                session_id: str | None = None) -> LaunchSpec:
    from swarm.supervisor.settings import settings
    s = settings(cfg)
    sid = session_id or str(uuid.uuid4())
    argv = [s["claude_bin"], "-p", "--session-id", sid, "--max-turns", str(s["max_turns"]),
            "--permission-mode", s["claude_permission_mode"], "--output-format", "json",
            "--setting-sources", "user"]
    if model:
        argv += ["--model", model]
    if s["max_budget_usd"] > 0:
        argv += ["--max-budget-usd", f"{s['max_budget_usd']:g}"]
    if s.get("claude_plugin_dir"):
        argv += ["--plugin-dir", s["claude_plugin_dir"]]
    check_safe(argv)
    return LaunchSpec("claude", tuple(argv), workdir, prompt, sid, minutes)


def codex_spec(cfg: dict, *, prompt: str, workdir: str, model: str | None, minutes: float) -> LaunchSpec:
    from swarm.supervisor.settings import settings
    s = settings(cfg)
    # no --cd: codex exec takes its process cwd as its working root when --cd is absent
    # (codex-rs/exec/src/lib.rs, `None => AbsolutePathBuf::current_dir()`), and the runner starts it
    # in the verified directory by descriptor (runner.open_workdir): a path would be re-resolved
    argv = [s["codex_bin"], "exec", "--json", "--sandbox", "workspace-write", "--skip-git-repo-check"]
    if model:
        argv += ["-m", model]
    n = s["codex_token_limit"]
    if n:
        argv += ["-c", "features.rollout_budget.enabled=true",
                 "-c", f"features.rollout_budget.limit_tokens={n}",
                 "-c", f"features.rollout_budget.reminder_at_remaining_tokens=[{max(1, n // 4)}]"]
    argv.append("-")
    check_safe(argv)
    return LaunchSpec("codex", tuple(argv), workdir, prompt, None, minutes)


def spec_for(cfg: dict, harness: str, **kw) -> LaunchSpec:
    """The launch spec for a replacement on `harness` ("codex" or "claude")."""
    if harness == "codex":
        kw.pop("session_id", None)
        return codex_spec(cfg, **kw)
    return claude_spec(cfg, **kw)
