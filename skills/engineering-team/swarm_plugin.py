"""The engineering-team plugin of the swarm CLI (loaded by swarm core from skills/*/swarm_plugin.py).

Adds `swarm team`, `swarm activate --team`, the team line of `swarm status --job J`, and reads the
default composition from team.toml. Core swarm knows nothing about teams: without this file those
commands do not exist and everything else works unchanged. See docs/PLUGINS.md for the plugin API.

Seats. Mandatory, always present and not removable: engineering_lead, qa, engineer (one or more),
judge. Optional, the user's choice: product_manager (default on), build_engineer (default off),
reviewer, verifier. A job's own composition (kept with the job on the board) wins over team.toml,
which wins over the built-in default.
"""
from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path

MANDATORY = ("engineering_lead", "qa", "engineer", "judge")
OPTIONAL = ("product_manager", "build_engineer", "reviewer", "verifier")
DEFAULT_OPTIONAL = ("product_manager",)
ABSENT = {   # who carries a missing optional seat's duties (shown by `swarm team --show`)
    "product_manager": "engineering_lead writes the change criteria compactly; engineering_lead and qa "
                       "record the product acceptance",
    "build_engineer": "engineering_lead owns the build, CI, merge-gate and build-slot duties",
}
DATA_KEY = "optional_roles"
FILE_ENV = "SWARM_TEAM_CONFIG"


class TeamError(ValueError):
    pass


def team_file(config_dir: Path) -> Path:
    return Path(os.environ.get(FILE_ENV) or config_dir / "team.toml").expanduser()


def check_roles(names, what: str = "role") -> list[str]:
    """The optional roles in `names`, validated: mandatory ones are accepted (already present),
    anything else unknown is a TeamError."""
    out = []
    for raw in names:
        name = raw.strip().lower()
        if not name:
            continue
        if name in MANDATORY:
            continue
        if name not in OPTIONAL:
            raise TeamError(f"unknown {what} {name!r}: optional roles are {', '.join(OPTIONAL)}; "
                            f"always present: {', '.join(MANDATORY)}")
        if name not in out:
            out.append(name)
    return out


def load_default(config_dir: Path) -> tuple[list[str], str]:
    """(optional roles, where from) of team.toml, or the built-in default when there is no file.
    `optional_roles = [...]`, at the top or under [team]. A broken file is a TeamError."""
    path = team_file(config_dir)
    if not path.is_file():
        return list(DEFAULT_OPTIONAL), "default"
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise TeamError(f"{path}: {exc}") from None
    section = data.get("team") if isinstance(data.get("team"), dict) else data
    roles = section.get(DATA_KEY, list(DEFAULT_OPTIONAL))
    if not isinstance(roles, list) or not all(isinstance(r, str) for r in roles):
        raise TeamError(f"{path}: optional_roles must be a list of role names")
    return check_roles(roles), str(path)


def stored(ctx, board, job: str) -> list[str] | None:
    raw = ctx.job_data(board, job).get(DATA_KEY)
    if raw is None:
        return None
    return [r for r in raw.split(",") if r in OPTIONAL]


def effective(ctx, board, job: str) -> tuple[list[str], str]:
    own = stored(ctx, board, job)
    if own is not None:
        return own, "this job"
    return load_default(ctx.config_dir)


def describe(optional: list[str], source: str) -> list[str]:
    lines = [f"mandatory  {', '.join(MANDATORY)} (engineer: one or more)",
             f"optional   {', '.join(optional) or 'none'}  (from {source})"]
    for role in ("product_manager", "build_engineer"):
        if role not in optional:
            lines.append(f"absent     {role}: {ABSENT[role]}")
    return lines


def _need_job(board, job: str) -> bool:
    js = board.job_status(job)
    if js is None:
        print(f"swarm team: no job {job!r}", file=sys.stderr)
        return False
    return True


def run_team(ctx, args) -> int:
    changes = bool(args.add or args.remove)
    try:
        add = check_roles(args.add or [])
        for raw in args.remove or []:
            name = raw.strip().lower()
            if name in MANDATORY:
                raise TeamError(f"{name} is mandatory and cannot be removed "
                                f"(always present: {', '.join(MANDATORY)})")
        remove = check_roles(args.remove or [])
        with ctx.open_board() as board:
            if not _need_job(board, args.job):
                return 1
            optional, source = effective(ctx, board, args.job)
            if changes:
                optional = [r for r in optional if r not in remove] + [r for r in add if r not in optional]
                if not ctx.set_job_data(board, args.job, DATA_KEY, ",".join(optional)):
                    print(f"swarm team: no job {args.job!r}", file=sys.stderr)
                    return 1
                source = "this job"
            print(f"team for {args.job}")
            for line in describe(optional, source):
                print(line)
    except TeamError as exc:
        print(f"swarm team: {exc}", file=sys.stderr)
        return 2
    return 0


def setup_team(parser) -> None:
    parser.description = ("Show or change a job's team composition. Always present: engineering_lead, qa, "
                          "engineer, judge (not removable). Optional: " + ", ".join(OPTIONAL) + ".")
    parser.add_argument("--job", required=True)
    parser.add_argument("--show", action="store_true", help="print the effective composition (the default)")
    parser.add_argument("--add", action="append", metavar="ROLE", help="add an optional role (repeatable)")
    parser.add_argument("--remove", action="append", metavar="ROLE",
                        help="remove an optional role (repeatable); a mandatory role is refused")


def setup_activate(parser) -> None:
    parser.add_argument("--team", metavar="ROLES",
                        help="the job's optional roles, comma-separated (e.g. product_manager,build_engineer; "
                             "'' for none); default: team.toml, else product_manager. Mandatory roles "
                             "(engineering_lead, qa, engineer, judge) are always present")


def parse_team_arg(value: str) -> list[str]:
    return check_roles(value.split(","), "role in --team")


def before_activate(ctx, args) -> int | None:
    if getattr(args, "team", None) is None:
        return None
    try:
        parse_team_arg(args.team)
    except TeamError as exc:
        print(f"swarm activate: --team: {exc}", file=sys.stderr)
        return 2
    return None


def after_activate(ctx, args) -> None:
    team = getattr(args, "team", None)
    with ctx.open_board() as board:
        if team is not None:
            ctx.set_job_data(board, args.job, DATA_KEY, ",".join(parse_team_arg(team)))
        optional, source = effective(ctx, board, args.job)
    print(f"team: mandatory {', '.join(MANDATORY)}; optional {', '.join(optional) or 'none'} (from {source}); "
          f"`swarm team --job {args.job} --show` before staffing")


def status_lines(ctx, job: str) -> list[str]:
    with ctx.open_board() as board:
        optional, source = effective(ctx, board, job)
    return [f"team       {', '.join((*MANDATORY, *optional))}  ({source})"]


def register(api) -> None:
    api.add_command("team", run_team, setup=setup_team,
                    help="show or change a job's team composition (engineering-team plugin)")
    api.extend_command("activate", setup=setup_activate, before=before_activate, after=after_activate)
    api.add_status_lines(status_lines)
