"""`swarm activate` in drive mode (spec 3.1, [A], [RR1], [M1], [G2], [F10], [D1], [P1]): the
budget and model tables, the supervisor checks, the pinned credential sources, the durable
five-step activation and its recovery, and `swarm drive budget|models`. Owned by Task 6a; the launch
commands (and `swarm halt|unhalt`) live in drive_cli.py.

Every control record this module creates prints, as its last stdout line, the record format of
the plan's Global Constraints: `swarm-record: kind=activate id=<generation> rev=<ledger revision>`.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

from swarm import enrolment, gitpin, hosts, paths
from swarm.supervisor import ledger
from swarm.supervisor import settings as sup_settings
from swarm.supervisor import driver_events
from swarm.supervisor.driver_model import DEFAULT_CFG

ROLE_MODELS = {"engineer": "haiku", "worker": "haiku", "integrator": "haiku", "reviewer": "sonnet",
               "qa": "sonnet", "judge": "opus", "lead": "opus", "security": "opus"}   # [P1] shipped defaults
DEFAULT_ALLOWED_MODELS = ("haiku", "sonnet", "opus")
BUDGET_KEYS = ("max_sessions", "max_rounds", "no_progress_rounds", "max_attempts",
               "max_cost_usd", "ruling_reserve_usd", "session_usd_cap", "min_session_usd",
               "max_session_minutes", "max_wall_hours", "ruling_attempts", "ruling_deadline_minutes",
               "ruling_grace_minutes", "stall_minutes", "driver_error_passes", "max_concurrent_sessions")
# [G2] the helper commands a pinned credential may name (gitpin owns the allowlist; it re-checks a
# pin read back from the owner record too). Anything else is refused at activation.
CREDENTIAL_HELPERS = gitpin.CREDENTIAL_HELPERS
NO_DRIVE_HINT = "pass --no-drive --reason TEXT to run it as a goal job that nothing drives"
_SESSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
_KV = re.compile(r"([a-z_]+)=(.+)", re.S)


class DriveError(ValueError):
    """A drive-mode request that is refused (the message says why and how to fix it)."""


# --- budgets and models ([D1], [P1]) -----------------------------------------------------------

def _drive(cfg: dict) -> dict:
    section = (cfg or {}).get("drive") or {}
    if not isinstance(section, dict):
        raise DriveError("[drive] must be a table of settings")
    return section


def _amount(key: str, value) -> float:
    """A finite number of at least 0 (a cost, a duration, a count), from a number or its text."""
    if isinstance(value, bool):
        raise DriveError(f"{key} must be a number, not {value!r}")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise DriveError(f"{key} must be a number, not {value!r}") from None
    if not math.isfinite(number) or number < 0:
        raise DriveError(f"{key} must be a finite number of at least 0, not {value!r}")
    return number


def budget_defaults(cfg: dict) -> dict:
    """The effective budgets from the shipped defaults overlaid by `[drive]` in the config."""
    drive = _drive(cfg)
    return {k: _amount(k, drive.get(k, DEFAULT_CFG[k])) for k in BUDGET_KEYS}


def check_budgets(budgets: dict) -> dict:
    """Refuse a reserve above the cap, and a cap below reserve + min_session (spec 4.1, [G7])."""
    cap, reserve = budgets["max_cost_usd"], budgets["ruling_reserve_usd"]
    minimum = budgets["min_session_usd"]
    if reserve > cap:
        raise DriveError(f"ruling_reserve_usd {reserve:g} is larger than max_cost_usd {cap:g}")
    if cap < reserve + minimum:
        raise DriveError(f"max_cost_usd {cap:g} is below ruling_reserve_usd + min_session_usd "
                         f"({reserve:g} + {minimum:g})")
    return budgets


def parse_budget_args(pairs, max_cost=None, reserve=None) -> dict:
    """KEY=VALUE pairs (`--budget`, `drive budget`) and the two flags, as a dict of amounts."""
    out: dict = {}
    for item in pairs or ():
        m = _KV.fullmatch(item or "")
        if m is None or m.group(1) not in BUDGET_KEYS:
            raise DriveError(f"budget {item!r}: the form is KEY=VALUE with KEY one of {', '.join(BUDGET_KEYS)}")
        out[m.group(1)] = _amount(m.group(1), m.group(2))
    if max_cost is not None:
        out["max_cost_usd"] = _amount("max_cost_usd", max_cost)
    if reserve is not None:
        out["ruling_reserve_usd"] = _amount("ruling_reserve_usd", reserve)
    return out


def effective_budgets(cfg: dict, overrides: dict | None = None) -> dict:
    """The job's budgets: the defaults, then the overrides (already parsed), validated."""
    budgets = budget_defaults(cfg)
    for key, value in (overrides or {}).items():
        if key not in BUDGET_KEYS:
            raise DriveError(f"unknown budget {key!r}")
        budgets[key] = _amount(key, value)
    return check_budgets(budgets)


def allowed_models(cfg: dict) -> tuple:
    value = _drive(cfg).get("allowed_models", list(DEFAULT_ALLOWED_MODELS))
    if not isinstance(value, list) or not all(isinstance(m, str) and m for m in value):
        raise DriveError("[drive] allowed_models must be a list of model names")
    return tuple(value)


def _check_model(role: str, model: str, allowed) -> None:
    if role not in ROLE_MODELS:
        raise DriveError(f"unknown role {role!r} (roles: {', '.join(ROLE_MODELS)})")
    if model not in allowed:
        raise DriveError(f"model {model!r} for {role} is not in [drive] allowed_models "
                         f"({', '.join(allowed)})")


def model_defaults(cfg: dict) -> dict:
    """The role -> model table: the shipped defaults overlaid by `[drive.models]`."""
    table = _drive(cfg).get("models", {})
    if not isinstance(table, dict):
        raise DriveError("[drive.models] must be a table of role = model")
    allowed = allowed_models(cfg)
    out = dict(ROLE_MODELS)
    for role, model in table.items():
        _check_model(role, model, allowed)
        out[role] = model
    for role, model in out.items():
        _check_model(role, model, allowed)
    return out


def parse_model_args(pairs) -> dict:
    out: dict = {}
    for item in pairs or ():
        m = _KV.fullmatch(item or "")
        if m is None:
            raise DriveError(f"model {item!r}: the form is ROLE=MODEL")
        out[m.group(1)] = m.group(2)
    return out


def effective_models(cfg: dict, overrides: dict | None = None) -> dict:
    """The job's role -> model table: `[drive.models]` (or the shipped defaults), then the job's
    overrides, each checked against the allowed models."""
    models = model_defaults(cfg)
    allowed = allowed_models(cfg)
    for role, model in (overrides or {}).items():
        _check_model(role, model, allowed)
        models[role] = model
    return models


# --- the driver precondition ([T21]) --------------------------------------------------------------

def _age_minutes(stamp, now: dt.datetime) -> float | None:
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        when = dt.datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    return (now - when).total_seconds() / 60.0


def driver_problem(cfg: dict) -> str | None:
    """Why a drive job cannot be activated on this host, or None. Spec 3.11 [T21]: no timer is needed or
    installed, on any platform; the session driver drives the job. Only the kill switch (`supervise.off`)
    refuses: every driver exits while it exists, so the job would never move."""
    off = sup_settings.switched_off()
    if off is not None:
        return f"the supervisor is switched off ({off} exists): remove it first, or {NO_DRIVE_HINT}"
    return None


# --- work roots, credentials, requirements -----------------------------------------------------

def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _real(path: str) -> str:
    return os.path.realpath(os.path.expanduser(path))


TMP_ROOTS = ("/tmp", "/private/tmp")   # never a work dir (spec 4.2); tests of positive activations narrow it


def is_tmp(path: str) -> bool:
    real = _real(path)
    return any(_under(real, t) for t in TMP_ROOTS)


def resolve_workdir(cfg: dict, given: str | None, cwd: str) -> str:
    """The job's work dir: --workdir, else the current directory. It must lie under
    [supervise] allowed_workdirs and never under /tmp ([A], spec 4.2)."""
    target = given if given else cwd
    if not os.path.isabs(os.path.expanduser(target)):
        raise DriveError(f"the work dir must be an absolute path, not {target!r}")
    real = _real(target)
    if is_tmp(real):
        raise DriveError(f"work dirs are never /tmp ({real}); pass --workdir DIR under "
                         f"[supervise] allowed_workdirs")
    roots = [_real(r) for r in sup_settings.settings(cfg)["allowed_workdirs"]]
    if not any(_under(real, r) for r in roots):
        hint = "" if given else "; the current directory is not under [supervise] allowed_workdirs: pass --workdir DIR"
        raise DriveError(f"{real} is not under [supervise] allowed_workdirs{hint}")
    return real


def blob_id(data: bytes) -> str:
    """The git blob id of a file's content (what GOAL-READY's pinned requirements compare)."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _pinned_url(url: str) -> str:
    try:
        return gitpin.check_url(url)
    except gitpin.GitPinError as exc:
        raise DriveError(str(exc)) from None


def sanitize_remote(url: str) -> str:
    """A remote's URL without its userinfo (user:password@), query string or fragment: a token is
    never stored or printed. An scp-style ssh remote (no scheme) has none of these and is returned as is.
    The result must be a URL a pinned git call takes (gitpin.check_url): never an option ("-..."), a
    transport helper (ext::) or another transport, and a local remote is an absolute path."""
    if not isinstance(url, str) or not url or not gitpin.no_controls(url) or url.startswith("-"):
        raise DriveError("a remote URL must not start with '-' or contain whitespace or control characters")
    if "://" not in url:
        return _pinned_url(url)
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        raise DriveError("a remote URL is not a valid URL") from None
    userinfo, at, hostport = parts.netloc.rpartition("@")
    # an ssh username is not a secret and is kept (ssh://git@host); a password or token never is, and
    # an https userinfo (often a bare token) is dropped whole
    user = userinfo.split(":", 1)[0] if at and parts.scheme == "ssh" else ""
    netloc = f"{user}@{hostport}" if user else hostport
    return _pinned_url(urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, "", "")))


def git_remotes(workdir: str) -> dict:
    """The fetch URLs of the work dir's git remotes, read once with the pinned git environment (no system
    or global config, hooks and fsmonitor off). The work dir's own config is launch-writable, so each URL
    is then checked like a --remote one: a relative local path is made absolute against the work dir, and
    a URL a pinned call refuses (an option, ext::, another transport) is left out, with a warning."""
    env = gitpin.git_env(home=str(paths.home()))
    try:
        res = subprocess.run(gitpin.git_argv("git", "-C", workdir, "remote", "-v"), capture_output=True,
                             text=True, timeout=30, env=env, cwd=gitpin.NEUTRAL_CWD)
    except (OSError, subprocess.SubprocessError):
        return {}
    if res.returncode != 0:
        return {}
    out: dict = {}
    for line in res.stdout.splitlines():
        parts = line.split("\t", 1)
        if len(parts) != 2 or not parts[1].endswith(" (fetch)"):
            continue
        name, url = parts[0], parts[1][:-len(" (fetch)")]
        if url and "://" not in url and not url.startswith("-") and not os.path.isabs(url) \
                and not gitpin.SCP_URL.match(url):
            url = os.path.normpath(os.path.join(workdir, url))
        try:
            out.setdefault(name, sanitize_remote(url))
        except DriveError as exc:
            print(f"swarm activate: remote {name[:80]!r} of the work dir is not pinned: {exc}", file=sys.stderr)
    return out


def credential_pin(spec: str) -> str:
    """One credential source, as pinned in the owner record: `helper:<absolute path>[ args]` from
    the allowlist, `ssh-key:<absolute path>`, or `ssh-agent` (the socket is pinned with it)."""
    if spec == "ssh-agent":
        sock = os.environ.get("SSH_AUTH_SOCK", "")
        if not os.path.isabs(sock):
            raise DriveError("ssh-agent: SSH_AUTH_SOCK is not set to an absolute socket path here")
        return check_pin(f"ssh-agent:{sock}")
    kind, _, value = spec.partition(":")
    if kind == "helper":
        exe = value.split(" ")[0]
        if not os.path.isabs(exe):
            raise DriveError(f"credential helper {spec!r}: pin an absolute path (helper:/abs/path/gh)")
        return check_pin(f"helper:{value}")
    if kind == "ssh-key":
        if not os.path.isabs(value):
            raise DriveError("ssh-key: pin an absolute key path")
        return check_pin(f"ssh-key:{value}")
    raise DriveError(f"credential {spec!r}: use helper:<absolute path>, ssh-key:<absolute path> or ssh-agent")


def check_pin(pin: str) -> str:
    """A stored credential pin, re-checked against the helper allowlist (gitpin.check_pin)."""
    try:
        return gitpin.check_pin(pin)
    except gitpin.GitPinError as exc:
        raise DriveError(str(exc)) from None


def check_config_dir(path: str, cfg: dict, roots=()) -> str:
    """[G2a] a gh helper's GH_CONFIG_DIR must be private to this user: refused under any [supervise]
    allowed_workdirs root or job root (a launch could plant hosts.yml there), when it is not this user's,
    when group or others can write it, or when a parent directory another user, or group or others, can
    change (gitpin.dir_problem)."""
    forbidden = [*sup_settings.settings(cfg)["allowed_workdirs"], *roots]
    problem = gitpin.dir_problem(path, forbidden)
    if problem:
        raise DriveError(f"the gh config dir {path} {problem}: point GH_CONFIG_DIR at a private directory")
    return path


_SCP_REMOTE = re.compile(r"(?:[^@/\\\s:]+@)?[^@/\\\s:]{2,}:")   # [user@]host:path (not a C:\ path)


def remote_kind(url: str) -> str:
    """local (file://, a plain path, git://), https, or ssh (ssh:// or scp-style [user@]host:path)."""
    if re.match(r"https?://", url):
        return "https"
    if re.match(r"ssh://", url):
        return "ssh"
    if "://" in url:
        return "local"
    return "ssh" if _SCP_REMOTE.match(url) else "local"


def default_credential(url: str) -> str | None:
    """The credential a remote gets when none is named: gh for https, the ssh agent for ssh, none for a
    local remote (file://, a plain path), which needs neither. A missing gh is an error that names the flag."""
    kind = remote_kind(url)
    if kind == "https":
        gh = shutil.which("gh")
        if gh is None or not os.path.isabs(gh):
            raise DriveError("no gh found for https remotes: pass --credential NAME=helper:/abs/path/to/gh")
        return credential_pin(f"helper:{os.path.abspath(gh)} auth git-credential")
    if kind == "ssh":
        return credential_pin("ssh-agent")
    return None


def write_lead_brief(workdir: str, job: str, text: str) -> Path:
    """The generated lead brief at <workdir>/.swarm/lead-brief-<job>.md, git-excluded ([G10])."""
    from swarm import safefile
    d = Path(workdir) / ".swarm"
    d.mkdir(exist_ok=True)
    path = d / f"lead-brief-{job}.md"
    safefile.write_preserving(path, text)
    git_exclude(workdir, ".swarm/")
    return path


def git_exclude(workdir: str, pattern: str) -> None:
    """Add `pattern` to the checkout's .git/info/exclude (a .git file's gitdir is followed)."""
    dotgit = Path(workdir) / ".git"
    if dotgit.is_file():
        text = dotgit.read_text(encoding="utf-8").strip()
        if not text.startswith("gitdir:"):
            return
        gitdir = Path(text.split(":", 1)[1].strip())
        dotgit = gitdir if gitdir.is_absolute() else Path(workdir) / gitdir
    if not dotgit.is_dir():
        return
    info = dotgit / "info"
    info.mkdir(exist_ok=True)
    exclude = info / "exclude"
    lines = exclude.read_text(encoding="utf-8").splitlines() if exclude.exists() else []
    if pattern not in lines:
        lines.append(pattern)
        exclude.write_text("\n".join(lines) + "\n", encoding="utf-8")


def lead_brief_text(job: str, goal: str, requirements: list) -> str:
    """The generated brief: the goal, the pinned requirements, the standing rules and the lead's task."""
    req = "\n".join(f"- {r}" for r in requirements) or "- (the goal text above)"
    return (f"# Lead brief for {job}\n\n## Goal\n\n{goal}\n\n## Requirements (pinned)\n\n{req}\n\n"
            "## Your task\n\nPlan the work and queue launches with `swarm run`, "
            "and post GOAL-READY when the target meets the requirements. Roles are instructions: reviewers and the "
            "judge only read. Targeted tests only, through /var/tmp/test-slots/test-slot when it exists; "
            "full suites only in CI; never put work trees or builds under /tmp; rebase-only landing.\n")


def gh_config_dir(env=None) -> str:
    """The GH_CONFIG_DIR a gh credential helper reads, resolved at activation and pinned per destination
    ([G2a]): $GH_CONFIG_DIR, else $XDG_CONFIG_HOME/gh, else ~/.config/gh. Refused when none of them can
    be determined, never guessed (a gh helper without its own config dir would read whatever is at $HOME)."""
    env = os.environ if env is None else env
    explicit = (env.get("GH_CONFIG_DIR") or "").strip()
    if explicit:
        if not os.path.isabs(explicit):
            raise DriveError("GH_CONFIG_DIR must be an absolute path for a gh credential helper")
        return os.path.normpath(explicit)
    xdg = (env.get("XDG_CONFIG_HOME") or "").strip()
    if xdg:
        if not os.path.isabs(xdg):
            raise DriveError("XDG_CONFIG_HOME must be an absolute path for a gh credential helper")
        return os.path.normpath(os.path.join(xdg, "gh"))
    home = os.path.expanduser("~")
    if not os.path.isabs(home):
        raise DriveError("the gh config dir cannot be determined (no home directory): set GH_CONFIG_DIR")
    return os.path.normpath(os.path.join(home, ".config", "gh"))


def pipeline_target(cfg: dict) -> str:
    """The landing target branch from [pipeline] (`target_branch`, else the `merge_target` alias)."""
    pipe = (cfg or {}).get("pipeline") or {}
    value = pipe.get("target_branch") or pipe.get("merge_target") or "main"
    try:
        return gitpin.check_branch(value)
    except gitpin.GitPinError:
        raise DriveError(f"[pipeline] target_branch {value!r} is not a branch name") from None


def remote_head(url: str, pin: str | None, config_dir: str | None, branch: str, *, timeout: int = 60) -> str | None:
    """The head SHA of `branch` on `url`, read with the destination's pinned credential (the same shape the
    mirror uses: the allowlisted helper with its GH_CONFIG_DIR, the ssh key, or the agent socket). None when
    the branch is absent. A failed read is a DriveError that names no URL and prints no stderr.
    The URL, the pin and the branch are checked again here (they may come back from the owner record), and
    git runs with the gitpin hygiene: `--` before the URL, the transports pinned, no system or global
    config, and a neutral working directory (never the launch-writable work dir)."""
    url = _pinned_url(url)
    if pin is not None:
        pin = check_pin(pin)
    try:
        branch = gitpin.check_branch(branch)
    except gitpin.GitPinError as exc:
        raise DriveError(str(exc)) from None
    extra: dict = {}
    args = ["-c", "credential.helper="]
    if pin is None:
        pass
    elif pin.startswith("helper:"):
        exe, *hargs = pin[len("helper:"):].split(" ")
        args += ["-c", "credential.helper=!" + " ".join(shlex.quote(x) for x in [exe, *hargs])]
        if config_dir:
            extra["GH_CONFIG_DIR"] = config_dir
    elif pin.startswith("ssh-key:"):
        extra["GIT_SSH_COMMAND"] = (f"ssh -i {shlex.quote(pin[len('ssh-key:'):])} -o IdentitiesOnly=yes "
                                    "-o BatchMode=yes -o StrictHostKeyChecking=yes")
    elif pin.startswith("ssh-agent:"):
        extra["SSH_AUTH_SOCK"] = pin[len("ssh-agent:"):]
        extra["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes -o StrictHostKeyChecking=yes"
    # a local remote runs the destination's own upload-pack: its config is defused there too
    pack = gitpin.local_pack_args("ls-remote") if gitpin.is_local(url) else []
    argv = gitpin.git_argv("git", *args, "ls-remote", "--heads", *pack, *gitpin.url_args(url, f"refs/heads/{branch}"))
    env = gitpin.git_env(home=str(paths.home()), extra=extra)
    try:
        res = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env,
                             cwd=gitpin.NEUTRAL_CWD)
    except (OSError, subprocess.SubprocessError):
        raise DriveError(f"could not reach a remote to read {branch} (see the remote's pin)") from None
    if res.returncode != 0:
        raise DriveError(f"could not read {branch} on a pinned remote (git exits {res.returncode})")
    for line in res.stdout.splitlines():
        sha, _, ref = line.partition("\t")
        if ref == f"refs/heads/{branch}" and re.fullmatch(r"[0-9a-f]{40,64}", sha):
            return sha
    return None


# --- the activation ([RR1]) --------------------------------------------------------------------

@dataclass
class Plan:
    """Everything activation decides before its first durable write."""
    job: str
    goal: str
    session: str | None
    harness: str
    workdir: str
    brief_path: str
    roots: list
    remotes: dict
    credentials: dict
    requirements: dict
    recipe: str
    budgets: dict
    overrides: dict          # the job's --model overrides (the ledger keeps these, not the table)
    review_role: str | None = "reviewer"    # None: the recipe has no review stage (stored as JSON null)
    review_gates: str | None = "standard"
    description: str | None = None
    target_branch: str = "main"           # [G3] pinned here, never read from config after activation
    config_dirs: dict = field(default_factory=dict)   # [G2a] destination -> GH_CONFIG_DIR of a gh helper


def board_key(cfg: dict) -> str:
    from swarm.board.autoinit import store_key
    return store_key(cfg)


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _save(bk: str, job: str, mutate) -> ledger.Ledger:
    """Load, change and save a ledger, again on a concurrent change (no silent overwrite)."""
    for _ in range(ledger.ACTIVATE_TRIES):
        led = ledger.load(bk, job)
        if led is None:
            raise DriveError(f"no drive ledger for job {job!r} on this host")
        mutate(led)
        try:
            led.save()
            return led
        except ledger.LedgerConflict:
            continue
    raise DriveError(f"job {job!r}: the ledger kept changing; try again")


def claim(board, plan: "Plan") -> None:
    """The board's job row and its drive claim `drive.enabled=1` ([RR1a]). Written before the ledger, and
    nothing writes it after the ledger: no state has a ledger without its claim."""
    status = board.job_status(plan.job)
    if status is None or status.status != "active":
        board.open_job(plan.job, plan.description, None, plan.session, os.environ.get("USER") or "person",
                       project="", goal=plan.goal)
    board.set_job_data(plan.job, "drive.enabled", "1")


def activate_drive(cfg: dict, board, plan: Plan, *, crash_after: str | None = None) -> ledger.Ledger:
    """Steps 1 to 4 of [RR1] for a new drive job, each durable before the next: the generated lead brief
    (if any), the host-private owner record, the board claim `drive.enabled=1` (with the job row), then
    the ledger in state "activating". The rest (the board data, the lead launch, the final "active") is
    finish_activation's, which the next pass also runs. `crash_after` is for tests."""
    bk = board_key(cfg)
    if plan.brief_path.startswith(plan.workdir + os.sep) and not os.path.exists(plan.brief_path):
        write_lead_brief(plan.workdir, plan.job, lead_brief_text(plan.job, plan.goal, sorted(plan.requirements)))
    if crash_after == "brief":
        raise RuntimeError("crash after brief")
    if _owner(bk, plan.job) is None:
        enrolment.write_job_owner(bk, job=plan.job, harness=plan.harness, session_id=plan.session,
                                  cwd=plan.workdir, roots=plan.roots, remotes=plan.remotes,
                                  credentials=plan.credentials, requirements=plan.requirements,
                                  recipe=plan.recipe, goal=plan.goal, brief=plan.brief_path,
                                  review_role=plan.review_role, review_gates=plan.review_gates,
                                  target_branch=plan.target_branch, config_dirs=plan.config_dirs)
    if crash_after == "owner":
        raise RuntimeError("crash after owner")
    claim(board, plan)
    if crash_after == "claim":
        raise RuntimeError("crash after claim")
    led = ledger.load(bk, plan.job)
    if led is None:
        led = ledger.create(bk, plan.job, budgets=plan.budgets, activated_at=_now(), state="activating",
                            models=plan.overrides, pinned=plan.requirements)
    if crash_after == "ledger":
        raise RuntimeError("crash after ledger")
    return led


def _owner(bk: str, job: str):
    """The job's owner record, None when there is none. One that exists but is of another format or has
    bad fields is a DRIVER-CONFIG hold that says so, never a silent "no record" (and never overwritten)."""
    try:
        return enrolment.find_job_owner(bk, job, strict=True)
    except enrolment.OwnerRecordInvalid as exc:
        raise DriveError(f"{driver_events.DRIVER_CONFIG}: job {job!r}: {exc}; it is held until `swarm doctor` "
                         f"and a person repair it") from None


def _record_activation_heads(cfg: dict, bk: str, job: str, o: dict) -> None:
    """[11b] the target's head on every pinned remote, recorded once per URL: the landing audit's `since`.
    A URL already recorded keeps its head (a rollover never overwrites it). A branch missing on a remote
    refuses the activation (the ledger stays "activating"; a retry runs this step again)."""
    target = o.get("target_branch") or "main"
    led = ledger.load(bk, job)
    if led is None:
        return
    pending = {}
    for name, url in sorted(o["remotes"].items()):
        if url in led.activation_heads or url in pending:
            continue
        pin, cdir = o["credentials"].get(name), o["config_dirs"].get(name)
        if pin is not None:
            check_pin(pin)                         # the allowlist again: the record is read back here
        if cdir is not None:
            check_config_dir(cdir, cfg, o["roots"])
        head = remote_head(url, pin, cdir, target)
        if head is None:
            raise DriveError(f"the target branch {target!r} is not on the remote {name!r}: push it there, "
                             f"then run `swarm activate` again")
        pending[url] = head
    if pending:
        _save(bk, job, lambda x: [x.activation_heads.setdefault(u, h) for u, h in pending.items()])


def finish_activation(cfg: dict, board, job: str, *, crash_after: str | None = None) -> ledger.Ledger | None:
    """Steps 5 to 7 of [RR1] for a job whose ledger is "activating" and whose owner record and board claim
    exist: the board's generation data, the lead launch, then the ledger "active".
    Idempotent: the activate command and the pass (Task 8a's reconcile) both run it, after any crash.
    `crash_after` is for tests ("data", "lead")."""
    bk = board_key(cfg)
    led = ledger.load(bk, job)
    if led is None or led.state != "activating":
        return led
    owner = _owner(bk, job)
    if owner is None:
        raise DriveError(f"{driver_events.DRIVER_ORPHAN}: job {job!r} has a drive ledger but no owner record on this host; "
                         f"it is held until `swarm doctor` and a person repair it")
    if owner.owner is None:
        raise DriveError(f"{driver_events.DRIVER_CONFIG}: job {job!r}: owner record format/fields invalid (it has no "
                         f"drive details); it is held until `swarm doctor` and a person repair it")
    if board.job_data(job).get("drive.enabled") != "1":
        # the claim is written before the ledger ([RR1a]); a ledger without it is not written here
        raise DriveError(f"{driver_events.DRIVER_ORPHAN}: job {job!r} has a drive ledger but no drive claim on the board; "
                         f"it is held until `swarm doctor` and a person repair it")
    o = owner.owner
    models = {**model_defaults(cfg), **led.models}
    session = owner.session_id
    user = os.environ.get("USER") or "person"
    board.set_job_data(job, "drive.generation", str(led.generation))
    board.set_job_data(job, "drive.generation_requested", str(led.generation_requested))
    if crash_after == "data":
        raise RuntimeError("crash after data")
    _record_activation_heads(cfg, bk, job, o)
    if crash_after == "heads":
        raise RuntimeError("crash after heads")
    lead = board.get_launch(job, led.generation, "lead")
    if lead is None:
        lead = board.queue_launch(job, led.generation, "lead", role="engineering_lead", harness=owner.harness,
                                  model=models["lead"], workdir=o["roots"][0], brief=o["brief"],
                                  queued_by=session or user)
    if crash_after == "lead":
        raise RuntimeError("crash after lead")

    def complete(x):
        x.state = "active"
        if x.requester_session is None and session:   # the requester session: the hooks and its driver ([T21])
            x.requester_session = session
    return _save(bk, job, complete)


def _session(args) -> str | None:
    session = args.session or hosts.cli_session_id(os.environ)
    if session is not None and not _SESSION.fullmatch(session):
        raise DriveError(f"--session must be a session id, not {session[:80]!r}")
    return session


def _split_pair(spec: str, flag: str) -> tuple:
    name, sep, value = spec.partition("=")
    if not sep or not name or not value:
        raise DriveError(f"{flag} {spec!r}: the form is NAME=VALUE")
    return name, value


def _requirements(args, goal: str, workdir: str) -> dict:
    """[F10]: each --requirements file's blob id, keyed by its real path; by default the goal text."""
    pinned = {"goal": blob_id(goal.encode("utf-8"))}
    for item in args.requirements or ():
        path = _real(item if os.path.isabs(os.path.expanduser(item)) else os.path.join(workdir, item))
        try:
            data = Path(path).read_bytes()
        except OSError as exc:
            raise DriveError(f"--requirements {item}: {exc.strerror or exc}") from None
        pinned[path] = blob_id(data)
    return pinned


def build_plan(cfg: dict, args, goal: str) -> Plan:
    """Decide the whole plan from the flags, the config and the work dir (nothing is written yet)."""
    if len(goal.encode("utf-8")) > enrolment.GOAL_MAX:
        raise DriveError(f"the goal is longer than {enrolment.GOAL_MAX} characters")
    workdir = resolve_workdir(cfg, args.workdir, os.getcwd())
    roots = [workdir] + [resolve_workdir(cfg, extra, os.getcwd()) for extra in (args.allow_workdir or ())]
    remotes = git_remotes(workdir)
    for spec in args.remote or ():
        name, url = _split_pair(spec, "--remote")
        remotes[name] = sanitize_remote(url)
    named = {}
    for spec in args.credential or ():
        dest, source = _split_pair(spec, "--credential")
        named[dest] = credential_pin(source)
    credentials = {}
    config_dirs = {}
    for name, url in remotes.items():
        pin = named.get(name) or default_credential(url)
        if pin is not None:
            credentials[name] = pin
            if pin.startswith("helper:") and os.path.basename(pin[len("helper:"):].split(" ")[0]) == "gh":
                # refused here when it cannot be determined, or when a launch could write it
                config_dirs[name] = check_config_dir(gh_config_dir(), cfg, roots)
    budgets = effective_budgets(cfg, parse_budget_args(args.budget, args.max_cost_usd, args.ruling_reserve_usd))
    overrides = parse_model_args(args.model)
    engineer = effective_models(cfg, overrides)["engineer"]   # validates the overrides too
    recipe = args.recipe or "coding"
    brief = _real(args.lead_brief) if args.lead_brief else str(Path(workdir) / ".swarm" / f"lead-brief-{args.job}.md")
    harness = args.lead_harness or hosts.detect_cli_host(os.environ) or "claude"
    return Plan(job=args.job, goal=goal, session=_session(args), harness=harness, workdir=workdir,
                brief_path=brief, roots=roots, remotes=remotes, credentials=credentials,
                requirements=_requirements(args, goal, workdir), recipe=recipe,
                budgets=budgets, overrides=overrides, description=args.description,
                review_role="reviewer" if recipe == "coding" else None,
                review_gates=("haiku-strict" if engineer == "haiku" else "standard") if recipe == "coding" else None,
                target_branch=pipeline_target(cfg), config_dirs=config_dirs)


def add_activate_args(parser) -> None:
    """The drive-mode flags of `swarm activate` (cli.py registers this once)."""
    parser.add_argument("--drive", action="store_true", help="accepted, a no-op: a goal implies drive")
    parser.add_argument("--no-drive", action="store_true", help="a goal nothing drives (not autonomous)")
    parser.add_argument("--reason", help="why --no-drive: required with it")
    parser.add_argument("--lead-brief", metavar="PATH", help="the lead's brief (default: generated)")
    parser.add_argument("--workdir", metavar="DIR", help="the job's work dir (default: this directory)")
    parser.add_argument("--allow-workdir", metavar="DIR", action="append", help="another root the job may use")
    parser.add_argument("--recipe", choices=["coding", "artifact"], help="coding (default) or artifact")
    parser.add_argument("--requirements", metavar="PATH", action="append", help="pin a requirements file")
    parser.add_argument("--remote", metavar="NAME=URL", action="append", help="pin a remote")
    parser.add_argument("--credential", metavar="DEST=SOURCE", action="append",
                        help="pin a credential: helper:/abs/gh, ssh-key:/abs/key or ssh-agent")
    parser.add_argument("--lead-harness", choices=["claude", "codex"], help="the lead's harness")
    parser.add_argument("--max-cost-usd", metavar="N", help="the job's spending cap")
    parser.add_argument("--ruling-reserve-usd", metavar="N", help="kept back for rulings")
    parser.add_argument("--budget", metavar="KEY=VALUE", action="append", help="any budget key")
    parser.add_argument("--model", metavar="ROLE=MODEL", action="append", help="a role's model for this job")


def add_drive_parser(sub) -> None:
    """`swarm drive budget|models` (person path or the requester session), on 6b's one `drive` parser tree
    (drive_cli.drive_subparsers: argparse refuses a second `drive` parser)."""
    from swarm import drive_cli
    dvs = drive_cli.drive_subparsers(sub)
    db = dvs.add_parser("budget", help="change budgets without a rollover: KEY=VALUE ...")
    db.add_argument("--job", required=True)
    db.add_argument("pairs", nargs="+", metavar="KEY=VALUE")
    dm = dvs.add_parser("models", help="change a role's model for the job: ROLE=MODEL ...")
    dm.add_argument("--job", required=True)
    dm.add_argument("pairs", nargs="+", metavar="ROLE=MODEL")
    drive_cli.add_drive_command("budget", cmd_drive, board=False)
    drive_cli.add_drive_command("models", cmd_drive, board=False)


def wants_drive(args) -> bool:
    """[A] a goal implies drive; --no-drive opts out (and needs --reason)."""
    return bool(args.goal) and not args.no_drive


def legacy_goal_check(args) -> int | None:
    """Before the legacy (not driven) path: --no-drive needs --reason, and says what it means.
    Returns an exit code to stop with, or None to carry on."""
    if not args.no_drive:
        return None
    if not (args.reason or "").strip():
        print("swarm activate: --no-drive needs --reason TEXT: say why the job is not driven", file=sys.stderr)
        return 2
    if args.goal:
        print(f"{args.job}: not driven by the supervisor (reason: {args.reason.strip()}): a person or a "
              f"coordinator must drive this goal job")
    return None


def kick() -> bool:
    """Wake the session drivers once a drive job is active (their wake file): the activation is already durable
    here, so a failing kick never undoes or fails it; it is reported on stderr (with its type). Returns whether
    it ran."""
    try:
        from swarm import drivegate
    except ModuleNotFoundError as exc:
        if exc.name in ("swarm.drivegate", "swarm"):
            return False                       # a build without the kick
        _kick_failed(exc)
        return False
    except Exception as exc:                   # a broken drivegate: say so, never hide it
        _kick_failed(exc)
        return False
    try:
        drivegate.kick()
    except Exception as exc:
        _kick_failed(exc)
        return False
    return True


def _kick_failed(exc: BaseException) -> None:
    print(f"swarm activate: WARNING: the driver kick failed ({type(exc).__name__}: {str(exc)[:200]}); the job "
          f"is active and its session driver picks it up; run `swarm doctor` if this repeats", file=sys.stderr)


def start_driver(cfg: dict, session: str | None, config_path) -> str:
    """Spec 3.11 [T21]: start the requester session's driver (one per session; it watches the nearest
    claude/codex process above this command). Says what happened in one line; never fails the activation."""
    try:
        from swarm.supervisor import session_driver
    except ImportError:
        return "no session driver in this build: run `swarm drive run`"
    what = session_driver.ensure(cfg, session, config_path=str(config_path) if config_path else None,
                                 activation=True)
    if what in ("started", "running"):
        return f"session driver {what} for session {session}"
    if what.startswith("refused"):
        return f"session driver not started ({what})"
    if what == "no host process":
        return ("no agent session process found above this command: the job advances while a session that "
                "owns it is open, or under `swarm drive run`")
    return f"session driver not started ({what}): the job advances under `swarm drive run`"


def cmd_activate_drive(cfg: dict, args) -> int:
    """`swarm activate --goal` (drive mode): refuse what this host cannot drive, else the durable
    activation, then the one record line. A re-activation of a drive job is a rollover request only."""
    from swarm.board import open_board
    problem = driver_problem(cfg)
    if problem:
        print(f"swarm activate: {problem}", file=sys.stderr)
        return 1
    try:
        plan = build_plan(cfg, args, args.goal.strip())
    except DriveError as exc:
        print(f"swarm activate: {exc}", file=sys.stderr)
        return 2
    bk = board_key(cfg)
    try:
        existing = ledger.load(bk, args.job)
        if existing is not None and existing.state == "active":
            refusal = _person("activate", cfg, args.job, args)   # re-activation is the person path ([F17])
            if refusal:
                print(f"swarm activate: {refusal}", file=sys.stderr)
                return 1
            reopen = existing.terminal is not None
            with open_board(cfg) as board:
                js = board.job_status(args.job)
                reopen |= js is not None and js.status in ("completed", "failed", "cancelled")
                if reopen:
                    # Reopening a finished job ([RR1] order: owner record, board claim, then the ledger): the
                    # owner record takes this activation's goal and pins, so the driver's tamper check sees the
                    # new goal as the owner's, and the closed board row opens again with that goal.
                    enrolment.write_job_owner(bk, job=plan.job, harness=plan.harness, session_id=plan.session,
                                              cwd=plan.workdir, roots=plan.roots, remotes=plan.remotes,
                                              credentials=plan.credentials, requirements=plan.requirements,
                                              recipe=plan.recipe, goal=plan.goal, brief=plan.brief_path,
                                              review_role=plan.review_role, review_gates=plan.review_gates,
                                              target_branch=plan.target_branch, config_dirs=plan.config_dirs)
                    claim(board, plan)
                led = ledger.activate(bk, args.job, budgets=plan.budgets, activated_at=_now(), reopen=reopen)
                board.set_job_data(args.job, "drive.generation_requested", str(led.generation_requested))
                if reopen:
                    from swarm.supervisor import driver_life
                    driver_life.rollover(board, cfg, led, board.now())
            print(f"{'reopened' if reopen else 're-activated'} {args.job}: generation {led.generation} -> "
                  f"{led.generation_requested} at the next pass")
            kick()
            print(start_driver(cfg, led.requester_session, getattr(args, "config", None)))
            print(f"swarm-record: kind=activate id={led.generation_requested} rev={led.ledger_rev}")
            return 0
        with open_board(cfg) as board:
            activate_drive(cfg, board, plan)
            led = finish_activation(cfg, board, args.job)
    except (DriveError, ledger.LedgerExistsError, ledger.LedgerFinishedError, ledger.LedgerBadError,
            ledger.LedgerConflict, OSError) as exc:
        print(f"swarm activate: {exc}", file=sys.stderr)
        return 1
    models = {**model_defaults(cfg), **led.models}
    kick()
    driven = start_driver(cfg, led.requester_session, getattr(args, "config", None))
    print(f"activated {args.job} (drive): the session driver drives it from here; the lead launch is queued "
          f"(generation {led.generation}); models {_models_text(models)}")
    print(driven)
    print(f"swarm-record: kind=activate id={led.generation} rev={led.ledger_rev}")
    return 0


def _models_text(models: dict) -> str:
    return " ".join(f"{r}={m}" for r, m in sorted(models.items()))


def budget_line(led: ledger.Ledger, now: dt.datetime | None = None) -> str:
    """`drive budget J: cost x/y USD (reserve r), sessions a/b, wall h/H h` (spec 4.1 [D1])."""
    from swarm.supervisor.driver_model import DEFAULT_CFG, session_count
    b = {**DEFAULT_CFG, **led.budgets}
    minutes = _age_minutes(led.activated_at, now or dt.datetime.now(dt.timezone.utc)) or 0.0
    return (f"drive budget {led.job}: cost {led.charged_usd():.2f}/{b['max_cost_usd']:.2f} USD "
            f"(reserve {b['ruling_reserve_usd']:.2f}), sessions {session_count(led)}/{b['max_sessions']:g}, "
            f"wall {minutes / 60:.1f}/{b['max_wall_hours']:g} h")


def drive_lines(cfg: dict, job: str) -> list:
    """The effective budget and model lines of an active drive job, for `swarm status --job J`. A ledger
    that cannot be read is said so (doctor reports it), never a crash of status."""
    try:
        led = ledger.load(board_key(cfg), job)
    except (ledger.LedgerBadError, OSError) as exc:
        return [f"drive      ledger of {job} unreadable ({exc}): run swarm doctor"]
    if led is None:
        return []
    from swarm.land import landing_required
    owner = enrolment.find_job_owner(board_key(cfg), job)
    details = getattr(owner, "owner", None)
    landing = ("landing: required (destinations pinned)" if landing_required(details)
               else "landing: none (no destination pinned)")
    from swarm.board import open_board
    data = {}
    if cfg.get("board"):
        with open_board(cfg) as board:
            data = board.job_data(job)
    waiting = []
    if led.terminal is None and led.watermarks.get("ruling-hold"):
        text = data.get("drive.ruling-hold", "ruling: needs a person; re-queue with swarm run or cancel")
        waiting += ["waiting on a person (ruling)",
                    f"waiting on: person ({text}; ruling-hold; see #{led.watermarks.get('ruling-hold-msg', 'unknown')})"]
    if led.terminal is None and led.watermarks.get("budget-hold"):
        cap = {**budget_defaults(cfg), **led.budgets}["max_cost_usd"]
        waiting += ["waiting on a person (budget)",
                   f"waiting on: person (budget: cost {led.charged_usd():.2f}/{cap:.2f} USD; "
                   f"raise with swarm drive budget --job {job} max_cost_usd=N; "
                   f"see #{led.watermarks.get('budget-hold-msg', 'unknown')}; "
                   f"budget-hold: {data.get('drive.budget-hold', 'budget exhausted')})"]
    if led.terminal is None and led.watermarks.get("goal-hold"):
        text = data.get("drive.goal-hold", f"goal-hold: needs a person; see #{led.watermarks.get('goal-hold-msg', 'unknown')}; "
                        f"re-queue with swarm run --job {job} --name lead ... or cancel")
        waiting.append(f"waiting on: person ({text})")
    if led.terminal is None and data.get("drive.waiting"):
        waiting.extend(line for line in data["drive.waiting"].splitlines()
                       if not any(led.watermarks.get(kind) and f"({kind}:" in line
                                  for kind in ("goal-hold", "ruling-hold", "budget-hold")))
    return waiting + [budget_line(led), f"drive models {job}: {_models_text({**model_defaults(cfg), **led.models})}", landing]


# --- person path and the requester session ([F17]) --------------------------------------------

def _person(action: str, cfg: dict, job: str, args=None) -> str | None:
    """The person path, through Task 6b's `swarm.person.require(action, job, *, board_key, env, confirm)`
    (returns the authority, or raises PermissionError): None when the action is allowed, else the refusal
    text. Until person.py is in the build the import fails and the action is refused."""
    try:
        from swarm import person
    except ImportError:
        return f"{action} needs the person path, which this build does not have (swarm/person.py, Task 6b)"
    bk = board_key(cfg)
    try:
        led = ledger.load(bk, job)
    except Exception:   # an unreadable ledger only means no attested caller; require() still decides
        led = None
    try:
        who = person.caller(bk, job, led, args) if args is not None else None
        person.require(action, job, board_key=bk, env=os.environ, who=who)
    except (PermissionError, person.PersonRefused) as exc:
        return str(exc) or f"{action} needs a person"
    return None


def clear_claim(board, job: str) -> None:
    """A terminal drive job drops its claim, so core rules stop treating it as driven ([RR1a]). Deactivate
    calls it; every other terminal state (the verdicts, Task 8b) must too."""
    board.set_job_data(job, "drive.enabled", None)


def cmd_drive(cfg: dict, args) -> int:
    """`swarm drive budget|models`: a change to a running drive job, with no generation rollover."""
    if getattr(args, "dcmd", None) not in ("budget", "models"):
        print("swarm drive: say budget or models", file=sys.stderr)
        return 2
    bk = board_key(cfg)
    led = ledger.load(bk, args.job)
    if led is None or led.terminal is not None:
        print(f"swarm drive: {args.job} is not an active drive job on this host", file=sys.stderr)
        return 1
    refusal = _person(f"drive {args.dcmd}", cfg, args.job, args)
    if refusal:
        print(f"swarm drive: {refusal}", file=sys.stderr)
        return 1
    try:
        if args.dcmd == "budget":
            if led.watermarks.get("ruling-hold"):
                print(f"warning: the ruling hold is not about budget; resume with swarm run --job {args.job} "
                      "--name lead ... or swarm activate --drive", file=sys.stderr)
            changes = parse_budget_args(args.pairs)
            merged = check_budgets({**led.budgets, **changes})
            _save(bk, args.job, lambda x: x.budgets.update(merged))
            print(f"budgets of {args.job} changed ({', '.join(sorted(changes))}); no generation change: "
                  f"max_cost_usd {merged['max_cost_usd']:g}, ruling_reserve_usd {merged['ruling_reserve_usd']:g}")
        else:
            picked = parse_model_args(args.pairs)
            effective_models(cfg, picked)   # validated before anything is stored
            _save(bk, args.job, lambda x: x.models.update(picked))
            print(f"models of {args.job}: {_models_text({**model_defaults(cfg), **led.models, **picked})}")
    except DriveError as exc:
        print(f"swarm drive: {exc}", file=sys.stderr)
        return 2
    return 0
