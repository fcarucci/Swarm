"""`swarm activate` in drive mode (spec 3.1, [A], [RR1], [M1], [G2], [F10], [D1], [P1]): the
budget and model tables, the supervisor checks, the pinned credential sources, the durable
five-step activation and its recovery, `swarm drive budget|models`, and the ledger halts that
`swarm supervise disable|enable` write. Owned by Task 6a; the seat commands live in drive_cli.py.

Every control record this module creates prints, as its last stdout line, the record format of
the plan's Global Constraints: `swarm-record: kind=activate id=<generation> rev=<ledger revision>`.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import math
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from swarm import enrolment, hosts, paths
from swarm.supervisor import ledger
from swarm.supervisor import settings as sup_settings
from swarm.supervisor.driver_model import DEFAULT_CFG

ROLE_MODELS = {"engineer": "haiku", "worker": "haiku", "integrator": "haiku", "reviewer": "sonnet",
               "qa": "sonnet", "judge": "opus", "lead": "opus", "security": "opus"}   # [P1] shipped defaults
DEFAULT_ALLOWED_MODELS = ("haiku", "sonnet", "opus")
BUDGET_KEYS = ("max_sessions", "max_seats", "max_rounds", "no_progress_rounds", "max_attempts",
               "max_cost_usd", "ruling_reserve_usd", "session_usd_cap", "min_session_usd",
               "max_session_minutes", "max_wall_hours", "ruling_attempts", "ruling_deadline_minutes",
               "ruling_grace_minutes", "stall_minutes", "driver_error_passes", "max_concurrent_sessions")
# [G2] the helper commands a pinned credential may name: the absolute executable's basename and
# the fixed arguments it takes. Anything else is refused at activation.
CREDENTIAL_HELPERS = {"gh": ("auth", "git-credential"), "git-credential-manager": (),
                      "git-credential-store": (), "git-credential-cache": ()}
NO_DRIVE_HINT = "pass --no-drive --reason TEXT to run it as a goal job that nothing drives"
HALT_REASON = "supervisor disabled"          # [F5]: the halt `supervise disable` writes
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


# --- the supervisor checks ([M1]) --------------------------------------------------------------

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


def supervisor_problem(cfg: dict, *, platform: str | None = None, timer: dict | None = None,
                       state: dict | None = None, now: dt.datetime | None = None) -> str | None:
    """Why a drive job cannot be activated on this host (None when it can): no timer on macOS and
    Windows; the supervisor disabled or switched off; the systemd user timer not active and
    enabled; no pass recorded within twice the timer period. The arguments exist for tests."""
    from swarm.supervisor import systemd
    plat = (PLATFORM or sys.platform) if platform is None else platform
    if plat == "darwin" or plat.startswith("win"):
        return f"drive jobs need the supervisor's systemd user timer, which this platform ({plat}) does not install; {NO_DRIVE_HINT}"
    if not sup_settings.enabled(cfg):
        return f"the supervisor is disabled on this host: run `swarm supervise enable` first, or {NO_DRIVE_HINT}"
    info = systemd.timer_state() if timer is None else timer
    if info.get("ActiveState") != "active" or info.get("UnitFileState") != "enabled":
        return (f"the supervisor timer {systemd.TIMER} is not running: run `swarm supervise enable`, "
                f"or {NO_DRIVE_HINT}")
    limit = 2 * sup_settings.settings(cfg)["timer_minutes"]
    when = now or dt.datetime.now(dt.timezone.utc)
    last = (sup_settings.load_state() if state is None else state).get("last_run_at")
    age = _age_minutes(last, when)
    if age is None or age > limit:
        return (f"no supervisor pass in the last {limit} minutes (last: {last or 'never'}); wait for the "
                f"timer's next pass, or {NO_DRIVE_HINT}")
    return None


# --- work roots, credentials, requirements -----------------------------------------------------

def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _real(path: str) -> str:
    return os.path.realpath(os.path.expanduser(path))


TMP_ROOTS = ("/tmp", "/private/tmp")   # never a work dir (spec 4.2); tests of positive activations narrow it
PLATFORM = None                        # the platform the supervisor check reads (None: sys.platform); tests set it


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


def sanitize_remote(url: str) -> str:
    """A remote's URL without its userinfo (user:password@), query string or fragment: a token is
    never stored or printed. An scp-style ssh remote (no scheme) has none of these and is returned as is."""
    if "://" not in url:
        return url
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        raise DriveError("a remote URL is not a valid URL") from None
    host = parts.netloc.rpartition("@")[2]
    return urllib.parse.urlunsplit((parts.scheme, host, parts.path, "", ""))


def git_remotes(workdir: str) -> dict:
    """The fetch URLs of the work dir's git remotes, read once with a sanitized environment."""
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(paths.home()), "LC_ALL": "C",
           "GIT_TERMINAL_PROMPT": "0"}
    try:
        res = subprocess.run(["git", "-C", workdir, "remote", "-v"], capture_output=True, text=True,
                             timeout=30, env=env)
    except (OSError, subprocess.SubprocessError):
        return {}
    if res.returncode != 0:
        return {}
    out: dict = {}
    for line in res.stdout.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[2] == "(fetch)":
            try:
                out.setdefault(parts[0], sanitize_remote(parts[1]))
            except DriveError:
                continue
    return out


def credential_pin(spec: str) -> str:
    """One credential source, as pinned in the owner record: `helper:<absolute path>[ args]` from
    the allowlist, `ssh-key:<absolute path>`, or `ssh-agent` (the socket is pinned with it)."""
    if spec == "ssh-agent":
        sock = os.environ.get("SSH_AUTH_SOCK", "")
        if not os.path.isabs(sock):
            raise DriveError("ssh-agent: SSH_AUTH_SOCK is not set to an absolute socket path here")
        return f"ssh-agent:{sock}"
    kind, _, value = spec.partition(":")
    if kind == "helper":
        exe, *args = value.split(" ")
        if not os.path.isabs(exe):
            raise DriveError(f"credential helper {spec!r}: pin an absolute path (helper:/abs/path/gh)")
        base = os.path.basename(exe)
        if base not in CREDENTIAL_HELPERS or tuple(args) != CREDENTIAL_HELPERS[base]:
            raise DriveError(f"credential helper {base!r} is not on the allowlist "
                             f"({', '.join(CREDENTIAL_HELPERS)}) with its fixed arguments")
        return f"helper:{value}"
    if kind == "ssh-key":
        if not os.path.isabs(value):
            raise DriveError("ssh-key: pin an absolute key path")
        return f"ssh-key:{value}"
    raise DriveError(f"credential {spec!r}: use helper:<absolute path>, ssh-key:<absolute path> or ssh-agent")


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
            "## Your task\n\nPlan the seats this goal needs, request each with `swarm seat request`, "
            "and post GOAL-READY when the target meets the requirements. Seats write; reviewers and the "
            "judge only read. Targeted tests only, through /var/tmp/test-slots/test-slot when it exists; "
            "full suites only in CI; never put work trees or builds under /tmp; rebase-only landing.\n")


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
    the ledger in state "activating". The rest (the board data, the lead seat, the final "active") is
    finish_activation's, which the next pass also runs. `crash_after` is for tests."""
    bk = board_key(cfg)
    if plan.brief_path.startswith(plan.workdir + os.sep) and not os.path.exists(plan.brief_path):
        write_lead_brief(plan.workdir, plan.job, lead_brief_text(plan.job, plan.goal, sorted(plan.requirements)))
    if crash_after == "brief":
        raise RuntimeError("crash after brief")
    if enrolment.find_job_owner(bk, plan.job) is None:
        enrolment.write_job_owner(bk, job=plan.job, harness=plan.harness, session_id=plan.session,
                                  cwd=plan.workdir, roots=plan.roots, remotes=plan.remotes,
                                  credentials=plan.credentials, requirements=plan.requirements,
                                  recipe=plan.recipe, goal=plan.goal, brief=plan.brief_path,
                                  review_role=plan.review_role, review_gates=plan.review_gates)
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


def finish_activation(cfg: dict, board, job: str, *, crash_after: str | None = None) -> ledger.Ledger | None:
    """Steps 5 to 7 of [RR1] for a job whose ledger is "activating" and whose owner record and board claim
    exist: the board's generation data, the lead seat request, then the ledger "active".
    Idempotent: the activate command and the pass (Task 8a's reconcile) both run it, after any crash.
    `crash_after` is for tests ("data", "lead")."""
    bk = board_key(cfg)
    led = ledger.load(bk, job)
    if led is None or led.state != "activating":
        return led
    owner = enrolment.find_job_owner(bk, job)
    if owner is None or owner.owner is None:
        raise DriveError(f"DRIVER-ORPHAN: job {job!r} has a drive ledger but no owner record on this host; "
                         f"it is held until `swarm doctor` and a person repair it")
    if board.job_data(job).get("drive.enabled") != "1":
        # the claim is written before the ledger ([RR1a]); a ledger without it is not written here
        raise DriveError(f"DRIVER-ORPHAN: job {job!r} has a drive ledger but no drive claim on the board; "
                         f"it is held until `swarm doctor` and a person repair it")
    o = owner.owner
    models = {**model_defaults(cfg), **led.models}
    session = owner.session_id
    user = os.environ.get("USER") or "person"
    board.set_job_data(job, "drive.generation", str(led.generation))
    board.set_job_data(job, "drive.generation_requested", str(led.generation_requested))
    if crash_after == "data":
        raise RuntimeError("crash after data")
    if board.seat(job, led.generation, "lead") is None:
        board.request_seat(job, led.generation, "lead", role="engineering_lead", harness=owner.harness,
                           model=models["lead"], workdir=o["roots"][0], brief_path=o["brief"],
                           requested_by=session or user)
    if crash_after == "lead":
        raise RuntimeError("crash after lead")
    return _save(bk, job, lambda x: setattr(x, "state", "active"))


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
    for name, url in remotes.items():
        pin = named.get(name) or default_credential(url)
        if pin is not None:
            credentials[name] = pin
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
                review_gates=("haiku-strict" if engineer == "haiku" else "standard") if recipe == "coding" else None)


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
    """`swarm drive budget|models` (person path or the requester session)."""
    dv = sub.add_parser("drive", help="change a running drive job: `drive budget`, `drive models`")
    dvs = dv.add_subparsers(dest="dcmd", required=True)
    db = dvs.add_parser("budget", help="change budgets without a rollover: KEY=VALUE ...")
    db.add_argument("--job", required=True)
    db.add_argument("pairs", nargs="+", metavar="KEY=VALUE")
    dm = dvs.add_parser("models", help="change a role's model for the job: ROLE=MODEL ...")
    dm.add_argument("--job", required=True)
    dm.add_argument("pairs", nargs="+", metavar="ROLE=MODEL")


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


def cmd_activate_drive(cfg: dict, args) -> int:
    """`swarm activate --goal` (drive mode): refuse what this host cannot drive, else the durable
    activation, then the one record line. A re-activation of a drive job is a rollover request only."""
    from swarm.board import open_board
    problem = supervisor_problem(cfg)
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
            refusal = _person("activate", cfg, args.job)   # re-activation is the person path ([F17])
            if refusal:
                print(f"swarm activate: {refusal}", file=sys.stderr)
                return 1
            led = ledger.activate(bk, args.job, budgets=plan.budgets, activated_at=_now())
            with open_board(cfg) as board:
                board.set_job_data(args.job, "drive.generation_requested", str(led.generation_requested))
            print(f"re-activated {args.job}: generation {led.generation} -> {led.generation_requested} "
                  f"at the next pass")
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
    print(f"activated {args.job} (drive): the supervisor drives it from here; the lead is seat 1 "
          f"(generation {led.generation}); models {_models_text(models)}")
    print(f"swarm-record: kind=activate id={led.generation} rev={led.ledger_rev}")
    return 0


def _models_text(models: dict) -> str:
    return " ".join(f"{r}={m}" for r, m in sorted(models.items()))


def budget_line(led: ledger.Ledger, now: dt.datetime | None = None) -> str:
    """`drive budget J: cost x/y USD (reserve r), sessions a/b, wall h/H h` (spec 4.1 [D1])."""
    b = led.budgets
    minutes = _age_minutes(led.activated_at, now or dt.datetime.now(dt.timezone.utc)) or 0.0
    return (f"drive budget {led.job}: cost {led.charged_usd():.2f}/{b['max_cost_usd']:.2f} USD "
            f"(reserve {b['ruling_reserve_usd']:.2f}), sessions {len(led.intents)}/{b['max_sessions']:g}, "
            f"wall {minutes / 60:.1f}/{b['max_wall_hours']:g} h")


def drive_lines(cfg: dict, job: str) -> list:
    """The effective budget and model lines of an active drive job, for `swarm status --job J`. A ledger
    that cannot be read is said so (doctor reports it), never a crash of status."""
    try:
        led = ledger.load(board_key(cfg), job)
    except (ledger.LedgerBadError, OSError) as exc:
        return [f"drive      ledger of {job} unreadable ({exc}): run swarm doctor"]
    if led is None or led.terminal is not None:
        return []
    return [budget_line(led), f"drive models {job}: {_models_text({**model_defaults(cfg), **led.models})}"]


# --- person path and the requester session ([F17]) --------------------------------------------

def _person(action: str, cfg: dict, job: str) -> str | None:
    """The person path, through Task 6b's `swarm.person.require(action, job, *, board_key, env, confirm)`
    (returns the authority, or raises PermissionError): None when the action is allowed, else the refusal
    text. Until person.py is in the build the import fails and the action is refused."""
    try:
        from swarm import person
    except ImportError:
        return f"{action} needs the person path, which this build does not have (swarm/person.py, Task 6b)"
    try:
        person.require(action, job, board_key=board_key(cfg))
    except PermissionError as exc:
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
    refusal = _person(f"drive {args.dcmd}", cfg, args.job)
    if refusal:
        print(f"swarm drive: {refusal}", file=sys.stderr)
        return 1
    try:
        if args.dcmd == "budget":
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


# --- swarm supervise disable|enable ([M1] 1 and 3, [F5]) -------------------------------------------

def refuse_seat_session(cfg: dict) -> str | None:
    """`supervise disable` is refused unless the person path allows it for every drive job of this board
    ([M1] 3): an enrolled seat session never does. With no drive job there is nothing to protect."""
    for led in ledger.unfinished(board_key(cfg)):
        if isinstance(led, ledger.Ledger):
            refusal = _person("supervise disable", cfg, led.job)
            if refusal:
                return refusal
    return None


def _halt(x, actor: str, when: str) -> None:
    x.halted = {"by": actor, "at": when, "reason": HALT_REASON}


def stop_job_sessions(cfg: dict, job: str) -> list:
    """Stop the live sessions of a job's seats (their scopes), as the runner does for a stop ([F5]).
    Returns what was stopped. The runner's run files are keyed by the config. Best effort per run: a
    run that cannot be read is skipped."""
    from swarm.supervisor import runner
    stopped = []
    for _restart, path in runner._run_paths(cfg):
        run = runner._read_run(path.name, cfg)
        if run and run.get("job") == job and runner._session_alive(run):
            runner._stop_session(run)
            stopped.append(run.get("unit") or path.name)
    return stopped


def halt_drive_jobs(cfg: dict, *, actor: str, now: dt.datetime | None = None) -> tuple:
    """`supervise disable`: the [F5] halt goes into every unfinished drive ledger of this board, then the
    live sessions of the halted jobs are stopped. A job whose ledger fails is reported and the others
    still get their halt. Returns (halted jobs, [(job, why)] failures)."""
    when = (now or dt.datetime.now(dt.timezone.utc)).isoformat()
    bk = board_key(cfg)
    halted, failed = [], []
    for led in ledger.unfinished(bk):
        if not isinstance(led, ledger.Ledger) or led.halted is not None:
            continue
        try:
            _save(bk, led.job, lambda x: _halt(x, actor, when))
            halted.append(led.job)
        except (DriveError, ledger.LedgerBadError, OSError) as exc:
            failed.append((led.job, str(exc)))
    for job in halted:
        try:
            stop_job_sessions(cfg, job)
        except Exception as exc:   # the halt stands; the pass stops what is left
            failed.append((job, f"sessions not stopped: {type(exc).__name__}: {exc}"))
    return halted, failed


def _clear_halt(x, seconds: float) -> None:
    x.halted_total_seconds += seconds
    x.halted = None


def unhalt_drive_jobs(cfg: dict, *, now: dt.datetime | None = None) -> tuple:
    """`supervise enable`: unhalt exactly the supervisor's halts; each deadline shifts by the time the
    job was halted ([F5]: halted_total_seconds). Returns (resumed jobs, [(job, why)] failures)."""
    when = now or dt.datetime.now(dt.timezone.utc)
    bk = board_key(cfg)
    done, failed = [], []
    for led in ledger.unfinished(bk):
        if not isinstance(led, ledger.Ledger) or not led.halted or led.halted.get("reason") != HALT_REASON:
            continue
        try:
            began = dt.datetime.fromisoformat(led.halted["at"])
            if began.tzinfo is None:
                began = began.replace(tzinfo=dt.timezone.utc)
            seconds = max(0.0, (when - began).total_seconds())
            _save(bk, led.job, lambda x, seconds=seconds: _clear_halt(x, seconds))
            done.append(led.job)
        except (DriveError, ledger.LedgerBadError, OSError, ValueError) as exc:
            failed.append((led.job, str(exc)))
    return done, failed
