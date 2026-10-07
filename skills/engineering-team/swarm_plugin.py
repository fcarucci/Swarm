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
import re
import shlex
import subprocess
import sys
import tomllib
from pathlib import Path

MANDATORY = ("engineering_lead", "qa", "engineer", "judge")
OPTIONAL = ("product_manager", "build_engineer", "reviewer", "verifier")
DEFAULT_OPTIONAL = ("product_manager",)
ABSENT = {   # who carries a missing optional seat's duties (shown by `swarm team --show`)
    "product_manager": "engineering_lead writes the change criteria compactly; engineering_lead and qa "
                       "record the product acceptance",
    "build_engineer": "engineering_lead owns the build, CI, integration-gate and build-slot duties",
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


def coding_artifact(artifact: str | None) -> tuple[str, str] | None:
    """Engineering artifacts are branch@full-sha; other references are generic."""
    if not isinstance(artifact, str):
        return None
    match = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9_./-]*)@([0-9a-fA-F]{40})", artifact)
    if not match:
        return None
    branch, sha = match.groups()
    if any(part.startswith(".") or part.endswith(".lock") or not part for part in branch.split("/")) or ".." in branch or branch.endswith("."):
        return None
    return branch, sha.lower()


def coding_settings(ctx, workdir: str | None = None) -> dict:
    """Team defaults, optional repository override by absolute work directory."""
    path = team_file(ctx.config_dir)
    data = {}
    if path.is_file():
        with path.open("rb") as stream:
            data = tomllib.load(stream)
    section = data.get("pipeline", {})
    if not isinstance(section, dict):
        raise TeamError("team [pipeline] must be a table")
    config = {"integrate": True, "merge_target": "main", "delete_branch": True,
              "evidence_command": "", "repository": "", **section}
    repos = data.get("repositories", {})
    if workdir and isinstance(repos, dict):
        override = repos.get(os.path.realpath(workdir), {})
        if not isinstance(override, dict):
            raise TeamError("team repository pipeline settings must be a table")
        config.update(override)
    for key in ("integrate", "delete_branch"):
        if not isinstance(config[key], bool):
            raise TeamError(f"team pipeline {key} must be true or false")
    config["target_branch"] = config.get("target_branch", config["merge_target"])
    if not coding_artifact(str(config["target_branch"]) + "@" + "0" * 40):
        raise TeamError("team pipeline target_branch must be a branch name")
    forge = data.get("forge", {})
    if not isinstance(forge, dict):
        raise TeamError("team [forge] must be a table")
    override_forge = config.get("forge", {})
    if not isinstance(override_forge, dict):
        raise TeamError("team repository forge settings must be a table")
    config["forge"] = {"kind": "none", "repository": config["repository"],
                       "evidence_command": config["evidence_command"], **forge, **override_forge}
    if config["forge"]["kind"] not in ("github", "gitea", "gitlab", "none"):
        raise TeamError("team forge kind must be github, gitea, gitlab or none")
    for key in ("repository", "evidence_command"):
        if not isinstance(config["forge"][key], str):
            raise TeamError(f"team forge {key} must be text")
    if config["forge"]["evidence_command"] and "{sha}" not in config["forge"]["evidence_command"]:
        raise TeamError("team forge evidence_command must check the exact {sha}")
    for key in ("evidence_command", "repository"):
        if not isinstance(config[key], str):
            raise TeamError(f"team pipeline {key} must be text")
    return config


def coding_integrated(workdir: str | None, branch: str, sha: str, target: str) -> bool:
    """Read Git facts only. Failed lookups never certify integration or branch deletion."""
    if not workdir:
        return False
    def git(*args):
        return subprocess.run(['git', *args], cwd=workdir, capture_output=True,
                              text=True, timeout=5)
    try:
        # A local tracking ref is sufficient positive ancestry evidence, even before fetch.
        main = git('rev-parse', '--verify', 'refs/remotes/origin/' + target)
        if main.returncode == 0 and git('merge-base', '--is-ancestor', sha, main.stdout.strip()).returncode == 0:
            return True
        # Exit 2 means a successful remote query found no matching branch. Auth/network
        # failures and missing origin use other codes and must keep the artifact pending.
        return git('ls-remote', '--exit-code', '--heads', 'origin', 'refs/heads/' + branch).returncode == 2
    except (OSError, subprocess.TimeoutExpired):
        return False


def coding_recipe(ctx, board, job: str, artifact: str | None) -> dict | None:
    from swarm.review import branch_revision
    parsed = branch_revision(artifact) if isinstance(artifact, str) else None
    if parsed is None:
        return None
    branch, sha = parsed
    # The activation record is host-private; a board/transcript cwd never selects config.
    from swarm.supervisor.orphans import local_record
    js = board.job_status(job)
    record = local_record(ctx.cfg, js) if js else None
    workdir = getattr(record, "workdir", None) or getattr(record, "cwd", None)
    config = coding_settings(ctx, workdir)
    if coding_integrated(workdir, branch, sha, config["target_branch"]):
        return {"artifact_group": branch, "integrated": True}
    if coding_artifact(artifact) is None:
        return None  # exact-SHA evidence requires the full SHA
    sha = sha.lower()
    evidence, evidence_check, forge_instructions = forge_adapter(config["forge"], workdir, artifact, branch, sha)

    finalize = (
        f"You are the INTEGRATOR for {artifact}, an executing role separate from the judge. "
        f"Recheck evidence on exact SHA {sha} before changing refs. Fetch every configured remote. "
        f"Verify branch {branch} still names {sha} on every remote that has it; a moved branch needs a new "
        "DONE and judge verdict, so stop integration and post the updated handoff. "
        f"In an isolated worktree on disk, rebase branch {branch} onto {config['target_branch']} "
        "using git rebase after fetching and reconciling the latest target from every remote. "
        "Integration always uses local rebase, then a fast-forward/push of the rebased branch; "
        "never create a merge commit, squash or force-push. If rebasing changes the SHA, push a fresh "
        "branch without rewriting published history, open/reuse a Merge Request / Pull Request where supported, "
        "then post a new DONE and judge verdict request for "
        "that exact rebased SHA, and stop this finalization. The old verdict and CI do not cover it. "
        "Open a Merge Request / Pull Request where the configured forge supports it; this is the "
        "review and CI vehicle. When no forge or request support exists, the rebased branch is pushed directly. "
        "Verify targeted checks and the project's CI on the exact rebased SHA using the configured forge adapter. "
        "Require a met verdict for that same SHA before advancing the target. If the target moves, rebase "
        "again and refresh the handoff, review and CI. Advance the target only by fast-forward/push of "
        "the approved rebased branch to every configured remote and push URL. "
        "If conflicts require design or code changes, abort the rebase and post FINALIZE_BLOCKED <artifact> <next steps> "
        "for the supervisor to hand back to a worker. "
        + (f"After every target push succeeds, delete {branch} from every configured remote and locally; "
           if config["delete_branch"] else "Keep the source branch; ")
        + f"run swarm learn and confirm durable outcome/learnings are retained; then post INTEGRATED {artifact} only after all required operations and learning succeed. "
        "Retain distilled learnings with swarm learn." + forge_instructions
    )
    return {"evidence_command": evidence, "evidence_check": evidence_check,
            "finalize": finalize, "finalizer_role": "integrator", "enabled": config["integrate"],
            "artifact_group": branch}


# Forge adapters: platform commands belong here, never in the generic coding recipe/core.
def github_repository(cwd: str | None) -> str:
    """Find GitHub even when the fetch remote is Gitea and GitHub is a push URL."""
    if not cwd:
        return ""
    result = subprocess.run(["git", "config", "--get-regexp", r"^remote\..*\.(url|pushurl)$"],
                            cwd=cwd, capture_output=True, text=True, timeout=5)
    repositories = set()
    if result.returncode == 0:
        for line in result.stdout.splitlines():
            _, _, url = line.partition(" ")
            match = re.fullmatch(r"(?:git@github\.com:|https://github\.com/|ssh://git@github\.com/)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?", url.strip())
            if match:
                repositories.add(match.group(1))
    # Several distinct repos require the explicit team/repository setting.
    return next(iter(repositories)) if len(repositories) == 1 else ""


def forge_adapter(forge: dict, workdir: str | None, artifact: str, branch: str, sha: str):
    """Exact revision CI and request procedures selected explicitly by [forge].

    Other adapters supply a project command: exit zero only for green CI on {sha}.
    An unconfigured checker holds integration; none still requires project CI evidence.
    """
    kind = forge["kind"]
    template = forge["evidence_command"]
    instructions = {
        "github": "\nForge adapter: github. Open/reuse a PR with gh pr create/view; inspect CI with gh run list --commit SHA. "
                  "Complete the PR only using a method that preserves the approved rebased SHA and linear history.",
        "gitea": "\nForge adapter: gitea. Open/reuse an MR/PR with tea pr create or the Gitea API; "
                 "check the project's CI for the exact SHA with the configured tea/API command. "
                 "Complete the request only using a method that preserves the approved rebased SHA and linear history.",
        "gitlab": "\nForge adapter: gitlab. Open/reuse an MR with glab mr create or the GitLab API; "
                  "check the project's CI for the exact SHA with the configured glab/API command. "
                  "Complete the MR only using a method that preserves the approved rebased SHA and linear history.",
        "none": "",
    }[kind]
    if template:
        command = template
        for key, value in (("artifact", artifact), ("branch", branch), ("sha", sha)):
            command = command.replace("{" + key + "}", shlex.quote(value))

        def check(cwd: str) -> bool:
            result = subprocess.run(command, shell=True, cwd=cwd, capture_output=True, text=True, timeout=30)
            return result.returncode == 0

        return command, check, instructions
    if kind != "github":
        return ("false # Configure [forge] evidence_command for the project's CI on exact SHA " + sha,
                lambda cwd: False, instructions)
    repository = forge["repository"] or github_repository(workdir)
    repo_args = ["--repo", repository] if repository else []
    argv = ["gh", "run", "list", "--commit", sha, "--limit", "100", *repo_args,
            "--json", "headSha,status,conclusion"]
    query = ('if length > 0 and all(.[]; .headSha == "' + sha + '" and '
             '.status == "completed" and .conclusion == "success") then "green" else "pending/red" end')
    command = '[ "$(' + shlex.join([*argv, "--jq", query]) + ')" = green ]'

    def check(cwd: str) -> bool:
        import json
        result = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=30)
        if result.returncode:
            return False
        runs = json.loads(result.stdout)
        return bool(isinstance(runs, list) and runs and all(
            isinstance(r, dict) and r.get("headSha") == sha and r.get("status") == "completed"
            and r.get("conclusion") == "success" for r in runs))

    return command, check, instructions

def register(api) -> None:
    api.add_command("team", run_team, setup=setup_team,
                    help="show or change a job's team composition (engineering-team plugin)")
    api.extend_command("activate", setup=setup_activate, before=before_activate, after=after_activate)
    api.add_status_lines(status_lines)
    api.add_pipeline_recipe(coding_recipe)
