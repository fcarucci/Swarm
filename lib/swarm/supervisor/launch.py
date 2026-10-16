"""How a replacement is started: `claude -p` with --max-turns and the
permission mode from [supervise] (never a bypass); `codex exec --sandbox workspace-write` with
the plugin's trusted hooks (never a bypass), optionally with the experimental rollout token
budget. Both get a wall clock from the runner. check_safe refuses any bypass flag.

Settings: a Claude replacement loads user settings only
(`--setting-sources user`, always, also with the e2e plugin dir): its work dir may hold a
.claude/settings.json planted by a sandboxed agent. check_safe refuses project or local setting
sources, always."""
from __future__ import annotations

import math
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


# Read-only launches (judges, reviewers) get no edit tool and no MCP tool: an MCP tool is outside the Bash
# sandbox, so the PreToolUse allowlist of read verbs (shellguard.mcp_read_allowed) is only the second line.
READ_ONLY_CLAUDE_DISALLOWED = "Edit,Write,NotebookEdit,MultiEdit,mcp__*"


def _budget(budget_usd) -> float:
    """A per-session budget in USD: finite and positive (never a zero or negative ceiling)."""
    b = float(budget_usd)
    if not math.isfinite(b) or b <= 0:
        raise ValueError(f"a session budget must be a positive number of USD, not {budget_usd!r}")
    return b


def _need_budget(budget_usd, drive: bool) -> None:
    """A drive launch never runs uncapped: its launch must carry a budget (the reservation)."""
    if drive and budget_usd is None:
        raise ValueError("a drive launch needs its budget_usd: it never runs without a cap")


def claude_spec(cfg: dict, *, prompt: str, workdir: str, model: str | None, minutes: float,
                session_id: str | None = None, budget_usd: float | None = None, read_only: bool = False,
                resume: str | None = None, drive: bool = False) -> LaunchSpec:
    from swarm.supervisor.settings import settings
    _need_budget(budget_usd, drive)
    s = settings(cfg)
    if resume:
        sid = str(resume)
        argv = [s["claude_bin"], "-p", "--resume", sid]
    else:
        sid = session_id or str(uuid.uuid4())
        argv = [s["claude_bin"], "-p", "--session-id", sid]
    argv += ["--max-turns", str(s["max_turns"]),
             "--permission-mode", s["claude_permission_mode"], "--output-format", "json",
             "--setting-sources", "user"]
    if model:
        argv += ["--model", model]
    if budget_usd is not None:
        argv += ["--max-budget-usd", f"{_budget(budget_usd):g}"]
    elif s["max_budget_usd"] > 0:
        argv += ["--max-budget-usd", f"{s['max_budget_usd']:g}"]
    if read_only:
        argv += ["--disallowedTools", READ_ONLY_CLAUDE_DISALLOWED]
    if s.get("claude_plugin_dir"):
        argv += ["--plugin-dir", s["claude_plugin_dir"]]
    check_safe(argv)
    return LaunchSpec("claude", tuple(argv), workdir, prompt, sid, minutes)


def _codex_tokens_for(cfg: dict, budget_usd: float) -> int:
    """floor(budget / codex_usd_per_mtok * 1e6): the rollout token limit a USD budget buys."""
    rate = float((cfg or {}).get("drive", {}).get("codex_usd_per_mtok", 10))
    if not math.isfinite(rate) or rate <= 0:
        raise ValueError("[drive] codex_usd_per_mtok must be a positive number")
    n = math.floor(_budget(budget_usd) / rate * 1_000_000)
    if n < 2:
        raise ValueError(f"a session budget of {budget_usd!r} USD is below the smallest rollout budget")
    return n


def codex_spec(cfg: dict, *, prompt: str, workdir: str, model: str | None, minutes: float,
               budget_usd: float | None = None, read_only: bool = False,
               resume: str | None = None, drive: bool = False) -> LaunchSpec:
    from swarm.supervisor.settings import settings
    _need_budget(budget_usd, drive)
    s = settings(cfg)
    # no --cd: codex exec takes its process cwd as its working root when --cd is absent
    # (codex-rs/exec/src/lib.rs, `None => AbsolutePathBuf::current_dir()`), and the runner starts it
    # in the verified directory by descriptor (runner.open_workdir): a path would be re-resolved
    sandbox = "read-only" if read_only else "workspace-write"
    if resume:
        # `codex exec resume <thread>`: the sandbox goes in as a config override (as hosts.codex
        # always passed it on resume)
        argv = [s["codex_bin"], "exec", "resume", "--json", "--skip-git-repo-check"]
    else:
        argv = [s["codex_bin"], "exec", "--json", "--sandbox", sandbox, "--skip-git-repo-check"]
    if model:
        argv += ["-m", model]
    if resume:
        argv += ["-c", f'sandbox_mode="{sandbox}"']
    n = _codex_tokens_for(cfg, budget_usd) if budget_usd is not None else s["codex_token_limit"]
    if n:
        argv += ["-c", "features.rollout_budget.enabled=true",
                 "-c", f"features.rollout_budget.limit_tokens={n}",
                 "-c", f"features.rollout_budget.reminder_at_remaining_tokens=[{max(1, n // 4)}]"]
    argv += [str(resume), "-"] if resume else ["-"]
    check_safe(argv)
    return LaunchSpec("codex", tuple(argv), workdir, prompt, None, minutes)


def spec_for(cfg: dict, harness: str, *, prompt: str, workdir: str, model: str | None, minutes: float,
             session_id: str | None = None, budget_usd: float | None = None, read_only: bool = False,
             resume: str | None = None, drive: bool = False) -> LaunchSpec:
    """The launch spec for a fresh replacement, or a resume, on `harness` ("codex" or "claude").
    The one contract for drive launches: model, sandbox or permission mode, turn and wall-clock
    limits, and the per-session budget are the same whether the session is new or resumed.
    `resume` is the harness's session handle (Claude session id; Codex thread id). `drive=True`
    (every drive launch) refuses a missing budget_usd: a drive session never runs uncapped."""
    kw = dict(prompt=prompt, workdir=workdir, model=model, minutes=minutes, budget_usd=budget_usd,
              read_only=read_only, resume=resume, drive=drive)
    if harness == "codex":
        return codex_spec(cfg, **kw)
    return claude_spec(cfg, session_id=session_id, **kw)
