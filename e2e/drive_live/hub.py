#!/usr/bin/env python3
"""A fake hub for landing tests: bare git repositories reached by file:// URLs, nothing else.

    hub.py init    --hub DIR --template TOYDIR [--dest a --dest b]   seed bare repos DIR/<dest>.git
    hub.py urls    --hub DIR                                         print NAME=file://... per destination
    hub.py ci-check --hub DIR [--run] SHA     exit 0 iff the CI result for exactly SHA is green
    hub.py ci-run  --hub DIR SHA              run `make test` on SHA in a clean clone and record it
    hub.py ci-fail --hub DIR SHA              record SHA as red (chaos: CI red on the rebased SHA)
    hub.py advance --hub DIR [--dest a]       push an unrelated commit to main (the target moves)
    hub.py audit   --hub DIR --since SHA      JSON: heads, linear history, destinations equal, commits

`ci-check` is shaped as a project evidence command (`[ci] kind = "none"`, `evidence_command`): it takes
the exact SHA, exits zero only for a recorded green result of that SHA, and with --run runs the checks
the way a CI triggered by a push would. Every destination refuses non-fast-forward updates and merge
commits on main (an `update` hook), so a landing that is not a plain fast-forward of a rebased branch fails
here and not only in a unit test. Branch deletion is allowed (the integrator deletes the source branch).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SHA = re.compile(r"^[0-9a-f]{40}$")
UPDATE_HOOK = """#!/bin/sh
# fake hub: main only moves by fast-forward and never receives a merge commit
ref="$1"; old="$2"; new="$3"
zero=0000000000000000000000000000000000000000
[ "$new" = "$zero" ] && exit 0
[ "$ref" = "refs/heads/main" ] || exit 0
if [ "$old" != "$zero" ]; then
  git merge-base --is-ancestor "$old" "$new" || { echo "hub: non-fast-forward refused" >&2; exit 1; }
  range="$old..$new"
else
  range="$new"
fi
if [ -n "$(git rev-list --merges "$range")" ]; then echo "hub: merge commit on main refused" >&2; exit 1; fi
exit 0
"""


def git(*a, cwd=None, check=True, env=None) -> str:
    e = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0",
         "GIT_AUTHOR_NAME": "hub", "GIT_AUTHOR_EMAIL": "hub@example.invalid",
         "GIT_COMMITTER_NAME": "hub", "GIT_COMMITTER_EMAIL": "hub@example.invalid", **(env or {})}
    r = subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True, env=e)
    if check and r.returncode:
        raise RuntimeError(f"git {' '.join(a)}: {r.stderr.strip()}")
    return r.stdout.strip()


def repo(hub: Path, dest: str) -> Path:
    if not re.fullmatch(r"[a-z0-9-]{1,32}", dest):
        raise SystemExit(f"bad destination name {dest!r}")
    return hub / f"{dest}.git"


def url(hub: Path, dest: str) -> str:
    return "file://" + str(repo(hub, dest).resolve())


def dests(hub: Path) -> list[str]:
    return sorted(p.name[:-4] for p in hub.glob("*.git"))


def init(hub: Path, template: Path, names: list[str]) -> dict:
    hub.mkdir(parents=True, exist_ok=True)
    seed = Path(tempfile.mkdtemp(prefix="seed-", dir=hub))
    try:
        shutil.copytree(template, seed / "w")
        w = seed / "w"
        git("init", "-q", "-b", "main", cwd=w)
        git("add", "-A", cwd=w)
        git("commit", "-q", "-m", "toy: initial", cwd=w)
        out = {}
        for d in names:
            r = repo(hub, d)
            git("init", "-q", "--bare", "-b", "main", str(r))
            git("config", "receive.denyNonFastForwards", "true", cwd=r)
            git("config", "receive.denyDeleteCurrent", "true", cwd=r)
            hook = r / "hooks" / "update"
            hook.write_text(UPDATE_HOOK)
            hook.chmod(0o755)
            git("push", "-q", url(hub, d), "main:refs/heads/main", cwd=w)
            out[d] = url(hub, d)
        out["initial"] = git("rev-parse", "HEAD", cwd=w)
        return out
    finally:
        shutil.rmtree(seed, ignore_errors=True)


def results_dir(hub: Path) -> Path:
    d = hub / "ci"
    d.mkdir(exist_ok=True)
    return d


def ci_run(hub: Path, sha: str, *, timeout: float = 120) -> dict:
    if not SHA.match(sha):
        raise SystemExit("ci needs the exact 40-hex SHA")
    src = next((d for d in dests(hub)
                if subprocess.run(["git", "cat-file", "-e", sha + "^{commit}"], cwd=repo(hub, d),
                                  capture_output=True).returncode == 0), None)
    res = {"sha": sha, "state": "failed", "at": time.time(), "detail": "sha not on any destination"}
    if src:
        tmp = Path(tempfile.mkdtemp(prefix="ci-", dir=hub))
        try:
            git("clone", "-q", "--no-checkout", str(repo(hub, src)), str(tmp / "w"))
            git("checkout", "-q", sha, cwd=tmp / "w")
            try:
                r = subprocess.run(["make", "test"], cwd=tmp / "w", capture_output=True, text=True,
                                   timeout=timeout, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
                res.update(state="green" if r.returncode == 0 else "failed",
                           detail=(r.stdout + r.stderr)[-600:])
            except subprocess.TimeoutExpired:
                res["detail"] = "make test timed out"
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    (results_dir(hub) / f"{sha}.json").write_text(json.dumps(res))
    return res


def ci_state(hub: Path, sha: str) -> str:
    p = results_dir(hub) / f"{sha}.json"
    if not SHA.match(sha) or not p.exists():
        return "none"
    return json.loads(p.read_text()).get("state", "none")


def advance(hub: Path, dest: str | None) -> dict:
    """One unrelated commit on main, pushed to every destination (or just `dest`): the target moved."""
    names = [dest] if dest else dests(hub)
    tmp = Path(tempfile.mkdtemp(prefix="adv-", dir=hub))
    try:
        git("clone", "-q", str(repo(hub, names[0])), str(tmp / "w"))
        (tmp / "w" / f"moved-{int(time.time() * 1000)}.txt").write_text("the target moved\n")
        git("add", "-A", cwd=tmp / "w")
        git("commit", "-q", "-m", "hub: unrelated change on main", cwd=tmp / "w")
        sha = git("rev-parse", "HEAD", cwd=tmp / "w")
        for d in names:
            git("push", "-q", url(hub, d), "main:refs/heads/main", cwd=tmp / "w")
        return {d: sha for d in names}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def audit(hub: Path, since: str | None) -> dict:
    heads, commits, linear = {}, [], True
    for d in dests(hub):
        r = repo(hub, d)
        heads[d] = git("rev-parse", "refs/heads/main", cwd=r, check=False)
        rng = f"{since}..main" if since else "main"
        commits = git("rev-list", "--reverse", rng, cwd=r, check=False).split()
        if git("rev-list", "--merges", rng, cwd=r, check=False):
            linear = False
    return {"heads": heads, "destinations_equal": len(set(heads.values())) == 1, "linear": linear,
            "commits": commits}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("init", "urls", "ci-check", "ci-run", "ci-fail", "advance", "audit"):
        s = sub.add_parser(name)
        s.add_argument("--hub", required=True)
        if name == "init":
            s.add_argument("--template", required=True)
            s.add_argument("--dest", action="append")
        if name in ("ci-check", "ci-run", "ci-fail"):
            s.add_argument("sha")
        if name == "ci-check":
            s.add_argument("--run", action="store_true")
        if name == "advance":
            s.add_argument("--dest")
        if name == "audit":
            s.add_argument("--since")
    a = p.parse_args(argv)
    hub = Path(a.hub).expanduser().absolute()
    if a.cmd == "init":
        print(json.dumps(init(hub, Path(a.template), a.dest or ["origin"])))
    elif a.cmd == "urls":
        for d in dests(hub):
            print(f"{d}={url(hub, d)}")
    elif a.cmd == "ci-run":
        res = ci_run(hub, a.sha)
        print(json.dumps(res)[:400])
        return 0 if res["state"] == "green" else 1
    elif a.cmd == "ci-fail":
        (results_dir(hub) / f"{a.sha}.json").write_text(json.dumps({"sha": a.sha, "state": "failed",
                                                                        "at": time.time(), "detail": "forced red"}))
    elif a.cmd == "ci-check":
        with open(results_dir(hub) / f"{a.sha}.checks", "a") as fh:   # who consulted CI on this exact SHA
            fh.write(f"{time.time()}\n")
        if ci_state(hub, a.sha) == "none" and a.run:
            ci_run(hub, a.sha)
        st = ci_state(hub, a.sha)
        print(st)
        return 0 if st == "green" else 1
    elif a.cmd == "advance":
        print(json.dumps(advance(hub, a.dest)))
    elif a.cmd == "audit":
        print(json.dumps(audit(hub, a.since)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
