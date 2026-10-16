"""`swarm land`: the trusted landing of a judged artifact (spec 3.8, 4.0 [G2] [F8] [F13] [F19] [F24]
[RR5] [RR8]). Two halves:

- request_land(): what `swarm land --job J --artifact BRANCH@SHA` runs in the integrator's session.
  It validates the artifact, posts the board message `LAND-REQUEST <artifact>` (its id is the
  request's record id; the hook receipts it as a `post`) and returns. The integrator does not wait.
- execute() / land(): what the driver runs in its pass, in code that takes no agent input for its
  decisions. It decides from the ledger, the owner record, receipts and the private mirrors:
  the request came from the generation's integrator seat; a receipted `met` verdict covers the exact
  SHA (or, for a rebase that changed nothing, its patch-id twin: the whole range's sorted multiset of
  patch-ids is equal); CI is green on it; the land leases for every destination are held. Then, per
  destination in sorted order, it journals `intent` -> `pushed` -> `verified` in a host-private `land`
  receipt (a crash between any two steps is completed by the next call, which is complete_land),
  fast-forwards the target to EXACTLY the judged SHA (never a force, never another SHA), verifies in
  the destination's private mirror, deletes the branch, writes the `integrated` receipt and the
  ledger's integrated[group] (landed, land_id, land_rev) and posts INTEGRATED. A refusal posts
  LAND-REFUSED <artifact> <why> (a wake event for the group's owner and the lead).

"Trusted" means decided by code that reads no agent input for its decisions; it is not a defence against
a same-user agent that rewrites the host-private files (spec 4.0)."""
from __future__ import annotations

import datetime as dt
import hashlib
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlsplit

from swarm import receipts
from swarm.supervisor import barrier, ledger, mirror as mirror_mod
from swarm.supervisor.mirror import (AuditDeadlineError, Mirror, MirrorError, MirrorTimeoutError,
                                     MissingCommitError, PushRejectedError, TargetRewoundError,
                                     UnpatchableCommitError)

REQUEST_PREFIX = "LAND-REQUEST "
INTEGRATED_PREFIX = "INTEGRATED "
REFUSED_PREFIX = "LAND-REFUSED "
REQUEST_KIND = "post"            # the receipt kind the hook writes for a LAND-REQUEST post (Task 5/5b)
LEASE_SECONDS = 600.0
FETCH_SECONDS = 50.0             # per destination, per pass; a timed-out fetch continues next pass
PUSH_SECONDS = 55.0
JOURNAL_DAYS = 14
_ARTIFACT = re.compile(r"([A-Za-z0-9][A-Za-z0-9._/-]{0,199})@([0-9a-f]{40})")
_ABS_PIN = re.compile(r"/[^\s\0]+")
SUPERVISOR_NAME = "swarm supervisor"


class LandRefused(Exception):
    """A request the landing refuses for good (the integrator must act): the message is the why."""


class LandPending(Exception):
    """Nothing is wrong, but the landing cannot finish now (lease held, fetch not done, CI not final)."""


class LeaseHeld(LandPending):
    """A land lease is not ours: another job, another host (never reclaimed), or not claimed yet."""


@dataclass(frozen=True)
class LandResult:
    ok: bool
    landed_sha: str | None
    why: str
    pending: bool = False
    land_id: str | None = None
    land_rev: int | None = None
    lease_held: bool = False


@dataclass(frozen=True)
class Destination:
    name: str
    url: str
    credential: dict


@dataclass(frozen=True)
class Request:
    """A LAND-REQUEST board message with the receipt that binds it to a seat."""
    id: int
    seat: str
    artifact: str
    generation: int


@dataclass
class LandEnv:
    """Everything `execute` decides from, injected so the driver (and tests) pass real or fake sources.

    led/owner: the host-private ledger and owner record; judged: the SHAs of the group's receipted `met`
    verdicts (from the driver's snapshot); ci_status(sha) -> "green"|"failed"|"running"|...; target: the
    branch to advance (the recipe's target branch)."""
    led: object
    owner: dict
    judged: Callable[[], set]
    ci_status: Callable[[str], str]
    target: str = "main"
    mirror_factory: Callable | None = None      # (url, credential) -> Mirror; None: Mirror with trusted_helpers
    now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.timezone.utc)
    config_dir: str | None = None               # the helper config location when the owner record pins none per destination
    delete_branch: bool = True
    left: Callable[[], float] = lambda: 1e9     # seconds left of the job's slot in the pass; every call is capped by it
    renew: Callable[[], None] = lambda: None    # renews the land leases (called before each network step)
    trusted_helpers: tuple = ()                 # absolute credential-helper paths the person named ([drive] credential_helpers)

    def __post_init__(self):
        # The target branch is pinned in the owner record at activation (6a): a mid-job config edit cannot redirect a landing.
        pinned = (self.owner or {}).get("target_branch")
        if isinstance(pinned, str) and pinned:
            self.target = pinned
    judged_branches: Callable[[], list] = lambda: []    # branches that carry judged SHAs (for a [F13] twin)
    say: Callable[[str], None] = lambda text: None


# --- artifacts, urls, leases -------------------------------------------------------------------

def parse_artifact(artifact) -> tuple[str, str]:
    """(branch, sha) of `BRANCH@<40 hex>`; ValueError for anything else (the exact SHA is required)."""
    m = _ARTIFACT.fullmatch(artifact) if isinstance(artifact, str) else None
    if m is None or ".." in m.group(1) or m.group(1).endswith(("/", ".lock")):
        raise ValueError("an artifact is BRANCH@<full 40-hex sha>")
    return m.group(1), m.group(2)


def normalize_url(url: str) -> str:
    """The normalised push URL a lease is keyed by: `host[:port]/path`, host lowercased, the scheme (https, ssh,
    scp-style) and the default port dropped, userinfo dropped, one trailing slash and a `.git` suffix dropped,
    so every spelling of one repository shares one lease; a file:// URL is its path."""
    text = url.strip()
    if "://" not in text:
        m = re.fullmatch(r"(?:[^@/:]+@)?([^/:\\]{2,}):(?!//)(.*)", text)
        if m is None:
            return os.path.normpath(text)
        text = f"ssh://{m.group(1)}/{m.group(2).lstrip('/')}"
    parts = urlsplit(text)
    host = (parts.hostname or "").lower()
    default = {"https": 443, "http": 80, "ssh": 22}.get(parts.scheme)
    port = f":{parts.port}" if parts.port and parts.port != default else ""
    path = parts.path.rstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    if parts.scheme.lower() == "file":
        return os.path.normpath(parts.path)
    return f"{host}{port}{path}"            # https, http and ssh spellings of one repository share a key


def lease_key(url: str, target: str) -> str:
    return f"land:{hashlib.sha256(normalize_url(url).encode()).hexdigest()}:{target}"


def lease_keys(dests: list, target: str) -> list[str]:
    """One key per destination (every pinned remote and push URL), sorted: claimed all-or-nothing."""
    return sorted({lease_key(d.url, target) for d in dests})


def credential_from_pin(pin: str | None, *, config_dir: str | None = None) -> dict:
    """The mirror's credential mapping for one owner-record pin: `helper:/abs/gh auth git-credential`,
    `ssh-key:/abs/key`, `ssh-agent:/abs/socket`, or None for a local destination. A helper needs its
    pinned config location (GH_CONFIG_DIR): there is no fallback to the driver's own $HOME."""
    if pin is None:
        return {"kind": "none"}
    kind, _, value = pin.partition(":")
    if kind == "helper":
        if not value or not os.path.isabs(value.split(" ", 1)[0]):
            raise LandRefused("the pinned credential helper is not an absolute path")
        if not config_dir:
            raise LandRefused("no helper config location (GH_CONFIG_DIR) is pinned for this destination")
        return {"kind": "helper", "command": value, "config_dir": config_dir}
    if kind == "ssh-key" and _ABS_PIN.fullmatch(value):
        return {"kind": "ssh-key", "path": value}
    if kind == "ssh-agent" and _ABS_PIN.fullmatch(value):
        return {"kind": "ssh-agent", "socket": value}
    raise LandRefused(f"the pinned credential {kind!r} is not usable (helper:/abs, ssh-key:/abs, ssh-agent:/abs)")


def destinations_from_owner(owner: dict, *, config_dir: str | None = None) -> list[Destination]:
    """The pinned destinations, sorted by normalised URL, duplicates (the same push URL under two
    remote names) collapsed. LandRefused when the owner record pins none."""
    remotes = (owner or {}).get("remotes") or {}
    creds = (owner or {}).get("credentials") or {}
    seen, out = set(), []
    for name in sorted(remotes):
        url = remotes[name]
        norm = normalize_url(url)
        if norm in seen:
            continue
        seen.add(norm)
        where = ((owner or {}).get("config_dirs") or {}).get(name) or config_dir
        out.append(Destination(name, url, credential_from_pin(creds.get(name), config_dir=where)))
    if not out:
        raise LandRefused("the owner record pins no destination to land on")
    return sorted(out, key=lambda d: normalize_url(d.url))


# --- the land journal and the integrated receipt ----------------------------------------------

def _epoch(now: dt.datetime) -> float:
    return now.timestamp()


def _journal_receipt(led, *, request: Request, action_id: str, rev: int, dests: dict, now) -> receipts.Receipt:
    return receipts.Receipt(
        board_key=led.board_key, job=led.job, generation=led.generation, incarnation=led.incarnation,
        action_id=action_id, attempt=1, seat=request.seat, kind="land", record_id=str(request.id), rev=rev,
        content_sha=receipts.content_sha("land", {"destinations": dests}),
        args_sha=receipts.args_sha(["swarm", "land", request.artifact]),
        expires_at=_epoch(now) + JOURNAL_DAYS * 86400, destinations=dests)


def read_journal(led, request_id, now, *, destinations: int):
    """The current land receipt of a request (the one file replaced on each step), or None. The rev is
    not known to a recovering reader, so the few it can be are tried (intent + pushed + verified per
    destination); a receipt whose content digest does not match its destinations is not returned."""
    for rev in range(1, 2 * destinations + 3):
        r = receipts.find(led.board_key, led.job, "land", str(request_id), rev=rev,
                          incarnation=led.incarnation, generation=led.generation, now=_epoch(now))
        if r is not None and r.destinations is not None and r.content_sha == receipts.content_sha(
                "land", {"destinations": r.destinations}):
            return r
    return None


def journal_verified(led, request_id, now, urls: list, sha: str) -> bool:
    """True when the land receipt shows every url `verified` at exactly `sha`."""
    r = read_journal(led, request_id, now, destinations=len(urls))
    return bool(r) and set(r.destinations) == set(urls) and all(
        v["state"] == "verified" and v["sha"] == sha for v in r.destinations.values())


# --- ledger updates ----------------------------------------------------------------------------

def update_ledger(led, fn) -> None:
    """Apply fn to the ledger and save it, reloading and retrying when another writer got in first
    (LedgerConflict); the caller's object ends up equal to what was stored."""
    for _ in range(ledger.ACTIVATE_TRIES):
        fn(led)
        try:
            led.save()
            return
        except ledger.LedgerConflict:
            fresh = ledger.load(led.board_key, led.job)
            if fresh is None:
                raise
            led.__dict__.update(fresh.__dict__)
    raise ledger.LedgerConflict(f"job {led.job!r}: the ledger kept changing")


# --- requests ----------------------------------------------------------------------------------

def request_text(artifact: str) -> str:
    return REQUEST_PREFIX + artifact


def request_land(board, job: str, artifact: str, *, name: str, agent_key: str | None = None) -> int:
    """The integrator's side of `swarm land`: validate the artifact and post `LAND-REQUEST <artifact>`.
    Returns the message id (the request's record id). ValueError for a malformed artifact."""
    parse_artifact(artifact)
    return board.post(job, name, request_text(artifact), agent_key=agent_key).id


def find_requests(board, env: LandEnv, job: str, *, after: int = 0) -> list[Request]:
    """The receipted LAND-REQUEST messages after id `after`, oldest first. A message without a valid,
    matching `post` receipt for this incarnation and generation is not a request: the board is
    writable by agents, so its existence proves nothing."""
    led, now = env.led, env.now()
    out = []
    for msg in board.messages_after(after, job):
        if not msg.message.startswith(REQUEST_PREFIX):
            continue
        artifact = msg.message[len(REQUEST_PREFIX):].strip()
        try:
            parse_artifact(artifact)
        except ValueError:
            continue
        r = receipts.find(led.board_key, job, REQUEST_KIND, str(msg.id), rev=1, incarnation=led.incarnation,
                          generation=led.generation, now=_epoch(now))
        row = {"kind": REQUEST_KIND, "id": msg.id, "rev": 1, "to": msg.to_agent, "body": msg.message}
        if r is None or not receipts.matches(r, row) or not r.seat:
            continue
        out.append(Request(msg.id, r.seat, artifact, r.generation))
    return out


_SLUG = re.compile(r"[^a-z0-9_-]+")


def integrator_seat(group: str) -> str:
    """The driver-owned integrator seat of an artifact group (the name driver_exec.seat_name gives it:
    `integrator-<slug of the group>-<8 hex digits of its hash>`; kept equal by test_seat_name_matches)."""
    slug = _SLUG.sub("-", (group or "").lower()).strip("-")[:30] or "group"
    return f"integrator-{slug}-{hashlib.sha256((group or '').encode()).hexdigest()[:8]}"


# --- the checks --------------------------------------------------------------------------------

def _check_request(env: LandEnv, request: Request, branch: str) -> None:
    led = env.led
    if request.generation != led.generation:
        raise LandRefused("the request belongs to an earlier generation of the job")
    if request.seat != integrator_seat(branch):
        raise LandRefused(f"the request is not from the integrator seat of {branch}")


def _twin(mirrors: dict, dests: list, target: str, sha: str, judged: set) -> bool:
    """[F13] True when `sha` changes exactly what some judged SHA changed: the sorted multiset of
    patch-ids of the range target..sha equals that of merge-base(judged, target)..judged, on every
    destination. A merge or empty commit in either range, or a missing commit, is not a twin."""
    for j in sorted(judged):
        ok = True
        for d in dests:
            m = mirrors[d.url]
            head = m.head(target)
            try:
                base_j = m.merge_base(j, head) if head else None
                if head is None or base_j is None:
                    ok = False
                    break
                ids = m.patch_ids(head, sha)
                if not ids or ids != m.patch_ids(base_j, j):      # an empty range (sha already on the target) is no twin
                    ok = False
                    break
            except (MissingCommitError, UnpatchableCommitError):
                ok = False
                break
        if ok:
            return True
    return False


def _covered(env: LandEnv, mirrors: dict, dests: list, target: str, sha: str) -> None:
    judged = env.judged()
    if sha in judged:
        return
    if _twin(mirrors, dests, target, sha, judged):
        return
    raise LandRefused(f"no receipted met verdict covers {sha[:12]} (a rebase that changes any patch-id needs a new "
                      f"hand-off and judge verdict)")


def _ci(env: LandEnv, sha: str) -> None:
    budget = _budget(env, 1e9)
    try:
        state = env.ci_status(sha, budget) if getattr(env.ci_status, "takes_timeout", False) else env.ci_status(sha)
    except Exception as exc:                        # a CI host outage is a hold, never a pass
        raise LandPending(f"CI status unavailable ({type(exc).__name__})") from exc
    if state == "green":
        return
    if state == "failed":
        raise LandRefused(f"CI is red on {sha[:12]}")
    raise LandPending(f"CI on {sha[:12]} is {state}, not green yet")


# --- the landing -------------------------------------------------------------------------------

def _budget(env: LandEnv, cap: float) -> float:
    """Seconds the next network step may take: min(cap, the job's slot left). LandPending when the slot is spent
    (the landing continues in the next pass). Renews the land leases first, so a long fetch cannot outlive them."""
    left = env.left()
    if left <= 0.5:
        raise LandPending("the job's time in this pass is spent; the landing continues next pass")
    env.renew()
    return min(cap, left)


def _make_mirror(env: LandEnv, d: Destination):
    if env.mirror_factory is not None:
        return env.mirror_factory(d.url, d.credential)
    return Mirror(d.url, d.credential, trusted_helper_paths=env.trusted_helpers)


def _mirrors(env: LandEnv, dests: list) -> dict:
    out = {}
    for d in dests:
        try:
            out[d.url] = _make_mirror(env, d)
        except MirrorError as exc:
            raise LandRefused(f"destination {d.name}: {exc}") from exc
    return out


def _pending_key(url: str, branch: str) -> str:
    return f"{hashlib.sha256(url.encode()).hexdigest()[:16]}:{branch}"


_GONE = "couldn't find remote ref"      # git's words for a branch the remote no longer has


def _fetch_why(env: LandEnv, d: Destination, exc: MirrorError) -> str:
    m = re.search(r"couldn't find remote ref refs/heads/(\S+)", str(exc))
    if m and m.group(1) == env.target:
        return f"{env.target} does not exist on {d.name}"
    if m:
        return (f"branch {m.group(1)} is not on {d.name}: hand off the exact SHA from the job's work "
                f"repository, then run swarm land again")
    return f"fetch from {d.name} failed: {exc}"


def publish_ref(sha: str) -> str:
    """The driver-owned branch a hand-off is published under: one per SHA (never a seat-chosen name, never moved,
    so no force is ever needed and a rebased re-hand-off gets its own)."""
    return f"swarm/land/{sha}"


def work_root_problem(root: str) -> str | None:
    """Why the pinned work root may not be read as the job's repository, or None. Its location and object store
    are seat-writable: a symlink, a `.git` gitdir file, a linked common dir, alternates, grafts or a shallow file
    could point the driver's fetch at another repository's commits, so each is refused."""
    import stat as _stat
    if not isinstance(root, str) or not os.path.isabs(root):
        return "not an absolute path"
    if os.path.realpath(root) != os.path.normpath(root):
        return "it is or passes through a symbolic link"
    g = os.path.join(root, ".git")
    try:
        st = os.lstat(g)
    except OSError:
        return "it has no .git directory"
    if not _stat.S_ISDIR(st.st_mode):
        return ".git is not a directory (a gitdir file or a link)"
    if hasattr(os, "getuid") and st.st_uid != os.getuid():
        return ".git belongs to another user"
    for rel in ("commondir", "objects/info/alternates", "objects/info/http-alternates", "info/grafts", "shallow"):
        if os.path.lexists(os.path.join(g, rel)):
            return f".git/{rel} exists"
    return None


def _stage(env: LandEnv, dests: list, mirrors: dict, branch: str, sha: str) -> set:
    """Seats never push (they hold no credentials, Task 20): a hand-off is a commit on a branch of the job's own
    work repository. Returns the urls of the destinations that do not carry `branch` at `sha`; for each, the
    commit is brought into the private mirror under publish_ref(sha): from the destination if the driver already
    published it there, else from a verified pinned work root. Nothing is pushed here (see _publish)."""
    pub = publish_ref(sha)
    staged = set()
    work = None
    for d in dests:
        m = mirrors[d.url]
        try:
            refs = m.ls_remote(_budget(env, FETCH_SECONDS))
        except MirrorError as exc:
            raise LandPending(f"cannot list {d.name} ({type(exc).__name__}); retrying next pass") from exc
        if refs.get(f"refs/heads/{branch}") == sha:
            continue
        staged.add(d.url)
        if refs.get(f"refs/heads/{pub}") == sha:
            _, left = m.fetch_branches([pub], _budget(env, FETCH_SECONDS))
            if left:
                raise LandPending(f"fetch of {pub} from {d.name} did not finish; it continues next pass")
            continue
        if work is None:
            work = _work_roots(env)
        got, why = None, "no pinned work root"
        for root in work:
            why = work_root_problem(root)
            if why is None:
                got = m.fetch_local_branch(root, branch, _budget(env, FETCH_SECONDS), into=pub)
                if got == sha:
                    break
        if got != sha:
            raise LandRefused(f"branch {branch} is not at {sha[:12]} in the job's work repository"
                              + (f" ({why})" if why else "") + ": commit there and hand off its exact SHA "
                              "(seats never push; the supervisor publishes it)")
    return staged


def _work_roots(env: LandEnv) -> list:
    return [r for r in ((env.owner or {}).get("roots") or []) if isinstance(r, str) and r]


def _publish(env: LandEnv, dests: list, mirrors: dict, staged: set, sha: str) -> None:
    """Push a judged (or twin-covered) SHA under publish_ref(sha) to the destinations that lack it: only after
    _covered, so no unjudged commit ever leaves the private mirror. Pinned credential, never a force."""
    pub = publish_ref(sha)
    for d in dests:
        if d.url not in staged:
            continue
        try:
            mirrors[d.url].push(sha, pub, timeout=_budget(env, PUSH_SECONDS))
        except PushRejectedError as exc:
            if "up to date" not in str(exc).lower():
                raise LandRefused(f"publishing {sha[:12]} to {d.name} failed: {exc.reason or exc}") from exc


def _fetch(env: LandEnv, dests: list, mirrors: dict, branches: list) -> None:
    """Bring `branches` into every destination's mirror; whatever does not arrive in time stays in the
    ledger's mirror_fetch_pending and LandPending is raised (the next pass continues it)."""
    waiting = []
    for d in dests:
        key = lambda b: _pending_key(d.url, b)
        try:
            _, left = mirrors[d.url].fetch_branches(branches, _budget(env, FETCH_SECONDS))
        except (MissingCommitError, TargetRewoundError, UnpatchableCommitError, MirrorTimeoutError):
            raise                                   # typed: execute() maps each to its own result
        except MirrorError as exc:
            raise LandRefused(_fetch_why(env, d, exc)) from exc
        waiting += [key(b) for b in left]
        done = {key(b) for b in branches if b not in left}
        update_ledger(env.led, lambda led: led.mirror_fetch_pending.__setitem__(
            slice(None), [x for x in led.mirror_fetch_pending if x not in done]))
    if waiting:
        update_ledger(env.led, lambda led: led.mirror_fetch_pending.extend(
            x for x in waiting if x not in led.mirror_fetch_pending))
        raise LandPending(f"fetch of {', '.join(branches)} did not finish in time; it continues next pass")


def check_leases(board, env: LandEnv, dests: list, job: str) -> None:
    """The land lease of every destination is held by this job on this host and has not expired.
    LandPending names the holder otherwise: a lease of another job or host is never taken over."""
    from swarm import compat
    keys = lease_keys(dests, env.target)
    held = {d.key: d for d in board.drive_locks("land:")}
    now = board.now()
    for key in keys:
        d = held.get(key)
        if d is None:
            raise LeaseHeld("the land lease is not claimed")
        if d.job != job or d.host != compat.node():
            where = "another host" if d.host != compat.node() else f"job {d.job}"
            raise LeaseHeld(f"land lease held by {where}")
        if d.expires_at <= now:
            raise LeaseHeld("the land lease expired before the landing finished")


def lease_holders(board, env: LandEnv, dests: list, job: str) -> list[str]:
    """Why the leases cannot be had: one phrase per key held by someone else (empty when all free/ours)."""
    from swarm import compat
    keys = set(lease_keys(dests, env.target))
    out = []
    for d in board.drive_locks("land:"):
        if d.key in keys and (d.job != job or d.host != compat.node()):
            out.append("another host" if d.host != compat.node() else f"job {d.job}")
    return sorted(set(out))


def execute(board, env: LandEnv, job: str, request: Request, *, action_id: str,
            need_leases: bool = True) -> LandResult:
    """Land one request, or finish an interrupted one. Raises nothing: refusals, holds and landed are
    all LandResults. Posts nothing (driver_land posts INTEGRATED / LAND-REFUSED from the result)."""
    try:
        return _execute(board, env, job, request, action_id, need_leases)
    except LandRefused as exc:
        return LandResult(False, None, str(exc))
    except LeaseHeld as exc:
        return LandResult(False, None, str(exc), pending=True, lease_held=True)
    except LandPending as exc:
        return LandResult(False, None, str(exc), pending=True)
    except MissingCommitError as exc:
        return LandResult(False, None, f"{exc}: hand off the exact SHA from the job's work repository, then run swarm land again")
    except TargetRewoundError as exc:
        return LandResult(False, None, f"the target was reset backwards ({exc})")
    except UnpatchableCommitError as exc:
        return LandResult(False, None, f"a merge or empty commit cannot be matched to a judged change ({exc})")
    except AuditDeadlineError as exc:
        return LandResult(False, None, str(exc), pending=True)
    except MirrorTimeoutError as exc:
        return LandResult(False, None, str(exc), pending=True)
    except PushRejectedError as exc:
        return LandResult(False, None, f"a destination refused the push: {exc.reason or exc}")
    except MirrorError as exc:
        return LandResult(False, None, f"git failed: {exc}")


def _execute(board, env: LandEnv, job: str, request: Request, action_id: str, need_leases: bool) -> LandResult:
    led, now = env.led, env.now()
    branch, sha = parse_artifact(request.artifact)
    target = env.target
    if branch == target:
        raise LandRefused("the artifact branch is the target branch")
    _check_request(env, request, branch)
    dests = destinations_from_owner(env.owner, config_dir=env.config_dir)
    urls = [d.url for d in dests]
    prior = led.integrated.get(branch) or {}
    if prior.get("landed") == sha and journal_verified(led, request.id, now, urls, sha):
        return LandResult(True, sha, "already landed", land_id=prior.get("land_id"), land_rev=prior.get("land_rev"))
    if need_leases:
        check_leases(board, env, dests, job)
    mirrors = _mirrors(env, dests)
    staged = _stage(env, dests, mirrors, branch, sha)
    _fetch(env, [d for d in dests if d.url not in staged], mirrors, [target, branch])
    _fetch(env, [d for d in dests if d.url in staged], mirrors, [target])
    pub = publish_ref(sha)
    journal = read_journal(led, request.id, now, destinations=len(dests))
    if journal is None:
        if sha not in env.judged():
            for extra in env.judged_branches():      # a twin needs the judged commits themselves
                for d in dests:
                    try:
                        _, left = mirrors[d.url].fetch_branches([extra], _budget(env, FETCH_SECONDS))
                    except MirrorError as exc:
                        if _GONE in str(exc):
                            continue             # a deleted judged branch is not an error
                        raise LandPending(f"fetch of judged branch {extra} failed ({type(exc).__name__}); "
                                          f"retrying next pass") from exc
                    if left:                     # a slow fetch is a hold, never "no verdict covers it"
                        raise LandPending(f"fetch of judged branch {extra} did not finish; it continues next pass")
        _covered(env, mirrors, dests, target, sha)
        _publish(env, dests, mirrors, staged, sha)
        _ci(env, sha)
        for d in dests:
            m = mirrors[d.url]
            if m.head(pub if d.url in staged else branch) != sha:
                raise LandRefused(f"{sha[:12]} is not in the mirror of {d.name}: hand off the exact SHA from the "
                                  f"job's work repository, then run swarm land again")
            head = m.head(target)
            if head is None:
                raise LandRefused(f"{target} does not exist on {d.name}")
            if not m.is_ancestor(sha, head) and not m.is_ancestor(head, sha):
                raise LandRefused(f"{target} on {d.name} moved: rebase {branch} onto {target} in the work repository and "
                                  f"hand off the new SHA (a fast-forward is impossible)")
        state = {d.url: {"state": "intent", "sha": sha} for d in dests}
        rev = 1
        receipts.write(led.board_key, _journal_receipt(led, request=request, action_id=action_id, rev=rev,
                                                       dests=state, now=now))
    else:
        if set(journal.destinations) != set(urls) or any(v["sha"] != sha for v in journal.destinations.values()):
            raise LandRefused("the land journal of this request names other destinations or another SHA")
        state, rev = {u: dict(v) for u, v in journal.destinations.items()}, journal.rev
        _ci(env, sha)
    first = True
    for d in dests:
        entry = state[d.url]
        if entry["state"] == "verified":
            continue
        m = mirrors[d.url]
        head = m.head(target)
        if head is None or not m.is_ancestor(sha, head):
            if head is None or not m.is_ancestor(head, sha):
                raise LandRefused(f"{target} on {d.name} moved: rebase {branch} onto {target} in the work repository and "
                                  f"hand off the new SHA (a fast-forward is impossible)")
            m.push(sha, target, timeout=_budget(env, PUSH_SECONDS))
        if entry["state"] == "intent":
            entry["state"] = "pushed"
            rev += 1
            receipts.write(led.board_key, _journal_receipt(led, request=request, action_id=action_id, rev=rev,
                                                           dests=state, now=env.now()))
        _, left = m.fetch_branches([target], _budget(env, FETCH_SECONDS))
        head = m.head(target)
        if left or head is None or not m.is_ancestor(sha, head):
            raise LandRefused(f"{d.name}: {target} does not contain {sha[:12]} after the push")
        entry["state"] = "verified"
        rev += 1
        receipts.write(led.board_key, _journal_receipt(led, request=request, action_id=action_id, rev=rev,
                                                       dests=state, now=env.now()))
        if first:
            first = False
            barrier.hit("land_after_first_destination")
    return _finish(board, env, job, request, branch, sha, dests, mirrors, rev, action_id)


def _finish(board, env: LandEnv, job: str, request: Request, branch: str, sha: str, dests: list, mirrors: dict,
            rev: int, action_id: str) -> LandResult:
    led, now = env.led, env.now()
    receipts.write(led.board_key, receipts.Receipt(
        board_key=led.board_key, job=job, generation=led.generation, incarnation=led.incarnation,
        action_id=action_id, attempt=1, seat=request.seat, kind="integrated", record_id=str(request.id), rev=1,
        content_sha=receipts.content_sha("integrated", {"group": branch, "sha": sha, "into": env.target}),
        args_sha=receipts.args_sha(["swarm", "land", request.artifact]),
        expires_at=_epoch(now) + JOURNAL_DAYS * 86400))

    def apply(l):
        ig = dict(l.integrated.get(branch) or {})
        for k in ("blocked", "blocked_id", "lease_held_since"):
            ig.pop(k, None)
        ig.update(landed=sha, land_id=str(request.id), land_rev=rev)
        l.integrated[branch] = ig
    update_ledger(led, apply)
    notes = []
    # Only the driver's own publish ref is removed, and only where it still names this exact SHA (compare-and-
    # delete). The hand-off branch name is the seat's: an unrelated branch of that name on a destination is never
    # touched (review of 4fda7fc).
    pub = publish_ref(sha)
    for d in dests:
        m = mirrors[d.url]
        try:
            if m.ls_remote(_budget(env, FETCH_SECONDS)).get(f"refs/heads/{pub}") != sha:
                continue
            m.delete_branch_at(pub, sha, timeout=_budget(env, PUSH_SECONDS))
        except MirrorError as exc:
            notes.append(f"{d.name}: {pub} not deleted ({exc})")
    for note in notes:
        env.say(note)
    return LandResult(True, sha, "landed", land_id=str(request.id), land_rev=rev)


def complete_land(board, env: LandEnv, job: str, request: Request, *, action_id: str,
                  need_leases: bool = True) -> LandResult:
    """Finish an interrupted landing from its journal (spec [F8], [RR5]): the same code, the same
    checks, for the destinations not yet verified."""
    return execute(board, env, job, request, action_id=action_id, need_leases=need_leases)


def land(board, env: LandEnv, job: str, artifact: str, *, caller_session: str, action_id: str | None = None,
         need_leases: bool = True) -> LandResult:
    """The synchronous form (plan interface): check that `caller_session` is the generation's integrator
    seat for the artifact's group, then execute. The driver uses execute() with the receipted request."""
    try:
        branch, _ = parse_artifact(artifact)
    except ValueError as exc:
        return LandResult(False, None, str(exc))
    seat = next((s for s in board.launches(job, env.led.generation)
                 if s.name == integrator_seat(branch)), None)
    if seat is None or not caller_session or seat.session_id != caller_session:
        return LandResult(False, None, f"the caller is not the integrator seat of {branch}")
    mine = [r for r in find_requests(board, env, job) if r.artifact == artifact and r.seat == seat.name]
    if not mine:
        return LandResult(False, None, "no receipted land request for this artifact from this seat: run swarm land")
    request = mine[-1]
    return execute(board, env, job, request, action_id=action_id or f"gen{env.led.generation}|land|-|{branch}|1|{request.id}",
                   need_leases=need_leases)


# --- the close-time target audit ([F24]) ---------------------------------------------------------

@dataclass(frozen=True)
class AuditResult:
    """findings: lines for DRIVER-AUDIT (empty = the target holds only judged work); pending: the audit ran
    out of time or could not fetch, so the close is retried in a later pass (nothing is decided)."""
    findings: tuple
    pending: bool = False


def audit(env: LandEnv, since: dict | None, candidate: str) -> AuditResult:
    """Audit every destination's target from its activation-time head (`since`: url -> sha) to `candidate`,
    in the private mirrors, against the judged SHAs. A flagged commit, a judged SHA the mirror lacks, a
    target reset behind its activation head and a merge/empty commit all become findings; a timeout or
    an overall-deadline overrun is `pending`."""
    dests = destinations_from_owner(env.owner, config_dir=env.config_dir)
    if since is None:                                   # the ledger's activation_heads: {remote url: sha}, written once
        since = dict(getattr(env.led, "activation_heads", None) or {})
    judged = set(env.judged())
    findings, pending = [], False
    for d in dests:
        base = since.get(d.url)
        if base is None:
            findings.append(f"{d.name}: no activation-time head is recorded for this destination")
            continue
        try:
            m = _make_mirror(env, d)
            _, left = m.fetch_branches([env.target], _budget(env, FETCH_SECONDS))
            if left:
                pending = True
                continue
            slow = False
            for extra in env.judged_branches():      # best effort: a landed branch is already deleted
                try:
                    _, more = m.fetch_branches([extra], _budget(env, FETCH_SECONDS))
                except MirrorError as exc:
                    if _GONE in str(exc):
                        continue
                    slow = True                  # a transient error: hold, do not judge on a partial mirror
                    continue
                slow = slow or bool(more)
            if slow:                                  # without a judged branch the audit would call its commits unjudged
                pending = True
                continue
            head = m.head(env.target)
            if head is None:
                findings.append(f"{d.name}: {env.target} does not exist")
                continue
            flagged = m.audit_target(base, head, judged, deadline=_budget(env, mirror_mod.AUDIT_DEADLINE))
        except MissingCommitError as exc:
            findings.append(f"{d.name}: a judged or activation commit is missing from the mirror ({exc})")
        except TargetRewoundError as exc:
            findings.append(f"{d.name}: {env.target} was reset backwards ({exc})")
        except UnpatchableCommitError as exc:
            findings.append(f"{d.name}: a merge or empty commit cannot be matched ({exc})")
        except (LandPending, MirrorTimeoutError):      # the slot is spent, or a timeout (AuditDeadlineError too)
            pending = True
        except MirrorError as exc:
            findings.append(f"{d.name}: the audit could not run ({exc})")
        else:
            findings += [f"{d.name}: {c[:12]} on {env.target} is not covered by any judged change" for c in flagged]
    return AuditResult(tuple(findings), pending and not findings)
