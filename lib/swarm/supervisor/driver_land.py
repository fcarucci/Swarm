"""The driver's side of landing (spec 3.8, [F8], [F19], [RR5], [RR8]): executes receipted LAND-REQUESTs in
the supervisor pass, claims and releases the per-destination land leases, finishes interrupted landings
(`complete_land`, also terminalization's step 0), and posts INTEGRATED or LAND-REFUSED.

Wiring (Task 8a/8b/8c): the pass builds a land.LandEnv for the job (ledger, owner record, the judged SHAs
from its snapshot, target branch) and calls `run(board, cfg, env, job)` once per pass, before any other
action on the group; terminalization calls `complete_all(...)` and posts DRIVER-PARTIAL-LAND from
`partial_report(...)` when a journal could not be completed."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

from swarm import compat, land
from swarm.land import LandEnv, LandResult, LeaseHeld, LandPending, Request
from swarm.supervisor.stuck import SUPERVISOR_NAME

WATERMARK = "land.request"
WHY_MAX = 300
CI_TIMEOUT = 25.0


class Held:
    """The lease claim of one landing: the launch that holds the keys, and the seat it borrowed."""

    def __init__(self, launch_id: str, holder: str, restart_id: int | None, seat_id: int | None, adopted: bool):
        self.launch_id, self.holder, self.restart_id, self.seat_id, self.adopted = (
            launch_id, holder, restart_id, seat_id, adopted)


def action_id(led, request: Request, branch: str) -> str:
    return f"gen{led.generation}|land|-|{branch}|1|{request.id}"


def claim_leases(board, cfg: dict, env: LandEnv, job: str, request: Request, dests: list, action: str) -> Held:
    """Claim every destination's land lease, all or none, through the atomic launch claim on the
    group's integrator seat. A lease of another job or host is never taken over (LeaseHeld). Leases an
    earlier, interrupted pass of this same landing left behind are adopted."""
    led = env.led
    branch, _ = land.parse_artifact(request.artifact)
    keys = land.lease_keys(dests, env.target)
    node, now = compat.node(), board.now()
    held = {d.key: d for d in board.drive_locks("land:") if d.key in set(keys)}
    if held:
        mine = (len(held) == len(keys) and all(d.job == job and d.host == node and d.holder == action
                                               and d.expires_at > now for d in held.values()))
        if not mine:
            raise LeaseHeld("land lease held by " + " and ".join(land.lease_holders(board, env, dests, job) or
                                                                  ["an earlier landing still to be fenced"]))
        first = next(iter(held.values()))
        row = next((r for r in board.restarts(job) if r.launch_id == first.launch_id and r.ended_at is None), None)
        return Held(first.launch_id, action, row.id if row else None, None, True)
    seat = board.seat(job, led.generation, land.integrator_seat(branch))
    if seat is None or seat.state not in ("requested", "idle"):
        raise LandPending("the integrator seat is not idle")
    attempt = 1 + sum(1 for r in board.restarts(job) if r.action_id == action)
    launch_id = f"land-{request.id}-{attempt}"
    restart = board.claim_drive_launch(
        job=job, generation=led.generation, action_id=action, attempt=attempt, launch_id=launch_id,
        seat_id=seat.id, seat_revision=seat.revision, harness=seat.harness, reserve_usd=0, reserve_minutes=1,
        locks=keys, lock_seconds=land.LEASE_SECONDS,
        max_host_running=int(cfg.get("max_concurrent_sessions", 4)) + 1)
    if restart is None:
        raise LeaseHeld("the land lease could not be claimed (held elsewhere, or the host's launch limit)")
    return Held(launch_id, action, restart.id, seat.id, False)


def release_leases(board, env: LandEnv, job: str, held: Held, outcome: str) -> None:
    board.release_drive_locks(held.launch_id, job=job, holder=held.holder)
    if held.restart_id is not None:
        board.finish_restart(held.restart_id, "completed" if outcome == "landed" else "failed")
    seat_id = held.seat_id
    if seat_id is None:
        return
    row = next((s for s in board.seats(job, env.led.generation) if s.id == seat_id), None)
    if row is not None and row.state == "running":
        try:
            board.set_seat_state(row.id, row.revision, "idle", job=job, last_outcome=f"land {outcome}")
        except ValueError:
            pass


def _text(prefix: str, artifact: str, why: str | None = None) -> str:
    if why is None:
        return f"{prefix}{artifact}"
    one_line = " ".join(why.split())[:WHY_MAX]
    return f"{prefix}{artifact} {one_line}"


def _set_lease_mark(env: LandEnv, branch: str, held: bool) -> None:
    """integrated[group].lease_held_since: set when a hold starts, kept while it lasts (the ruling clock), cleared
    when the hold ends."""
    if held == ("lease_held_since" in (env.led.integrated.get(branch) or {})):
        return

    def apply(led):
        ig = dict(led.integrated.get(branch) or {})
        if held:
            ig["lease_held_since"] = env.now().isoformat()
        else:
            ig.pop("lease_held_since", None)
        led.integrated[branch] = ig
    land.update_ledger(env.led, apply)


def execute_request(board, cfg: dict, env: LandEnv, job: str, request: Request) -> LandResult:
    """One request: claim the leases, land (or resume), release, post the outcome. A pending result posts
    nothing and keeps the request; the lease hold is marked on integrated[group] for the ruling clock."""
    branch, sha = land.parse_artifact(request.artifact)
    try:
        dests = land.destinations_from_owner(env.owner, config_dir=env.config_dir)
    except land.LandRefused as exc:
        res = LandResult(False, None, str(exc))
        board.post(job, SUPERVISOR_NAME, _text(land.REFUSED_PREFIX, request.artifact, res.why))
        return res
    action = action_id(env.led, request, branch)
    try:
        held = claim_leases(board, cfg, env, job, request, dests, action)
    except LeaseHeld as exc:
        _set_lease_mark(env, branch, True)
        return LandResult(False, None, str(exc), pending=True, lease_held=True)
    except LandPending as exc:
        return LandResult(False, None, str(exc), pending=True)
    outcome = "failed"
    try:
        res = land.execute(board, env, job, request, action_id=action)
        outcome = "landed" if res.ok else "failed"
    finally:
        release_leases(board, env, job, held, outcome)
    _set_lease_mark(env, branch, res.lease_held)
    if res.ok:
        board.post(job, SUPERVISOR_NAME, _text(land.INTEGRATED_PREFIX, request.artifact))
    elif not res.pending:
        board.post(job, SUPERVISOR_NAME, _text(land.REFUSED_PREFIX, request.artifact, res.why))
    return res


def _advance(env: LandEnv, request_id: int) -> None:
    land.update_ledger(env.led, lambda led: led.watermarks.__setitem__(
        WATERMARK, max(request_id, led.watermarks.get(WATERMARK, 0))))


def run(board, cfg: dict, env: LandEnv, job: str) -> list:
    """Execute the job's pending land requests, oldest first, once. Returns [(Request, LandResult)].
    A pending request (lease held, fetch or CI not done) stops the loop and is retried next pass;
    a landed or refused one moves the watermark past it."""
    done = []
    after = env.led.watermarks.get(WATERMARK, 0)
    for request in land.find_requests(board, env, job, after=after):
        res = execute_request(board, cfg, env, job, request)
        done.append((request, res))
        if res.pending:
            break
        _advance(env, request.id)
    return done


def _unfinished(board, env: LandEnv, job: str) -> list:
    """Requests with a land journal that is not complete, or complete but not recorded landed."""
    out = []
    now = env.now()
    for request in land.find_requests(board, env, job, after=0):
        branch, sha = land.parse_artifact(request.artifact)
        if (env.led.integrated.get(branch) or {}).get("landed") == sha:
            continue
        dests = land.destinations_from_owner(env.owner, config_dir=env.config_dir)
        if land.read_journal(env.led, request.id, now, destinations=len(dests)) is not None:
            out.append(request)
    return out


def complete_all(board, cfg: dict, env: LandEnv, job: str) -> list:
    """[RR5] step 0 of terminalization, and [F8] before any other action on a group: finish every
    interrupted landing from its journal. Returns [(Request, LandResult)]."""
    return [(r, execute_request(board, cfg, env, job, r)) for r in _unfinished(board, env, job)]


def partial_report(board, env: LandEnv, job: str) -> list[str]:
    """`<url>@<sha>` for every destination a journal shows pushed or verified whose landing never
    completed: what DRIVER-PARTIAL-LAND names in the verdict details."""
    out, now = [], env.now()
    for request in _unfinished(board, env, job):
        j = land.read_journal(env.led, request.id, now, destinations=64)
        for url, v in sorted((j.destinations if j else {}).items()):
            if v["state"] in ("pushed", "verified"):
                out.append(f"{url}@{v['sha']}")
    return out


# --- CI -----------------------------------------------------------------------------------------

_SLUG = re.compile(r"(?:https?://[^/]+/|ssh://[^/]+/|[^/@:]+@[^:]+:)([\w.-]+/[\w.-]+?)(?:\.git)?/?$")


def repo_slug(url: str) -> str | None:
    """`owner/name` of a GitHub-style remote URL, else None."""
    m = _SLUG.match(url or "")
    return m.group(1) if m else None


def ci_status_cli(repo: str | None, *, run=subprocess.run):
    """ci_status(sha) -> "green"|"failed"|"running"|"queued"|"none" through `swarm ci status --json` (the
    ci plugin's shared poller), asked as this host user. A CLI that fails raises (the landing holds)."""
    def status(sha: str) -> str:
        if not repo:
            raise ValueError("no CI repository is known: set [land] ci_repo")
        swarm = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))), "bin", "swarm")
        proc = run([swarm, "ci", "status", "--repo", repo, "--sha", sha, "--json"], capture_output=True,
                   text=True, timeout=CI_TIMEOUT, stdin=subprocess.DEVNULL)
        if proc.returncode != 0:
            raise OSError(f"swarm ci status exited {proc.returncode}")
        try:
            state = json.loads(proc.stdout.strip().splitlines()[-1])["state"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise OSError("swarm ci status printed no state") from exc
        return state if isinstance(state, str) else "unknown"
    return status
