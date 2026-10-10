"""`swarm ci status|wait`: budget-safe CI state for an exact SHA (ci plugin).

Agents call this INSTEAD of `gh run watch` / `gh run list` loops. Every agent on a box shares one
cache and one rate floor, so N waiting agents cost the host the same as one:

  cache   ~/.cache/swarm/ci/<owner>/<repo>/<sha>.json (the last result), `_poll.json` (the repo's
          poll state) and `.lock`. One process holds the lock and polls; the others read the cache.
  floor   at most one CI host call per repo per 60 s across all agents. Queued or not-yet-started
          runs back off 60, 120, then 300 s. Fewer than 500 API calls left: 10 minutes.
  budget  a 403 or a low X-RateLimit-Remaining reads `gh api rate_limit` (free). On a 403 the poller
          switches to the unauthenticated public API (60/h per IP) under the same floor, and says so.
  events  with the event core installed (`swarm event`), a CI event for the exact SHA ends the wait
          at once, with no CI host call; polling is the fallback.

CI hosts come from `[ci] kind` in team.toml (github, gitea); nothing is assumed. Secrets are paths
(`token_file`), never values. Exit codes of `wait`: 0 green, 1 failed, 124 timeout, 2 usage/config.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

try:
    import fcntl
except ImportError:   # Windows: msvcrt byte-range lock below
    fcntl = None

FLOOR_S = 60
BACKOFF_S = (60, 120, 300)
LOW_BUDGET = 500
LOW_BUDGET_S = 600
PUBLIC_MODE_S = 900          # how long a 403 keeps the poller on the public API
TTL_GREEN_S = 600            # a cached green is served this long; a failure is never served from cache
                             # on its own: a re-run may have started, so it is re-checked (floor-limited)
WANT_S = 180                 # a waiter's interest lasts this long unless it renews
TICK_S = 5                   # waiters wake this often to read the cache or an event
SHA_RE = re.compile(r"[0-9a-fA-F]{7,64}")
REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
GOOD = {"success", "skipped", "neutral"}
BAD = {"failure", "cancelled", "timed_out", "action_required", "startup_failure", "stale"}
CI_EVENT_KINDS = ("CI-FAILED", "READY-TO-LAND", "CI-GREEN", "CI-PASSED", "CI-SUCCESS", "CI-RESULT")


class CiError(Exception):
    pass


class RateLimited(Exception):
    def __init__(self, remaining=0, reset=None):
        super().__init__("rate limited")
        self.remaining, self.reset = remaining, reset


def parse_duration(text: str) -> float:
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*", str(text))
    if not m:
        raise CiError(f"bad duration {text!r} (use 30s, 90m, 2h)")
    return float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


def result(state, runs=(), failing=(), remaining=None, tail="", **extra):
    return {"state": state, "runs": list(runs), "failing": list(failing), "remaining": remaining,
            "tail": tail, **extra}


def classify(runs) -> str:
    """none | queued | running | green | failed, from run dicts {status, conclusion}."""
    if not runs:
        return "none"
    if any(r.get("status") == "completed" and r.get("conclusion") in BAD for r in runs):
        return "failed"
    if all(r.get("status") == "completed" for r in runs):
        return "green" if all(r.get("conclusion") in GOOD for r in runs) else "failed"
    if any(r.get("status") == "in_progress" for r in runs) or any(r.get("status") == "completed" for r in runs):
        return "running"
    return "queued"


def newer_attempt(prior, res) -> bool:
    """True when the host's runs carry a higher run_attempt than the cached failure's (a re-run).
    Hosts without run_attempt (Gitea) never report one."""
    def top(r):
        vals = [x.get("attempt") for x in (r or {}).get("runs", []) if isinstance(x.get("attempt"), int)]
        return max(vals) if vals else None
    a, b = top(prior), top(res)
    return a is not None and b is not None and b > a


# ---------------------------------------------------------------- CI hosts
def _read_token(path) -> str:
    return Path(str(path)).expanduser().read_text().strip()


def _http_json(url, headers=None, timeout=30):
    req = urllib.request.Request(url, headers={"Accept": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace")), resp.headers
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 429):
            h = exc.headers or {}
            raise RateLimited(_int(h.get("X-RateLimit-Remaining"), 0), _int(h.get("X-RateLimit-Reset")))
        raise CiError(f"{url.split('?')[0]}: HTTP {exc.code}")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise CiError(f"{url.split('?')[0]}: {exc}")


def _int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class GitHubHost:
    """gh api (authenticated, the caller's gh login) or the public API when `public` is set."""
    name = "github"

    def __init__(self, run=subprocess.run, http=_http_json):
        self.run, self.http = run, http

    def _gh(self, *argv):
        p = self.run(["gh", "api", *argv], capture_output=True, text=True, timeout=60)
        return p.returncode, p.stdout or "", p.stderr or ""

    def fetch(self, repo, sha, public=False):
        path = f"repos/{repo}/actions/runs?head_sha={sha}&per_page=100"
        if public:
            data, headers = self.http("https://api.github.com/" + path)
            remaining = _int(headers.get("X-RateLimit-Remaining"))
        else:
            rc, out, err = self._gh("-i", path)
            head, _, body = out.replace("\r\n", "\n").partition("\n\n")
            if rc != 0 and ("403" in err or "403" in head.split("\n", 1)[0] or "rate limit" in err.lower()):
                raise RateLimited(0, None)
            if rc != 0:
                raise CiError(f"gh api: {(err or head).strip()[:200]}")
            m = re.search(r"(?im)^x-ratelimit-remaining:\s*(\d+)", head)
            remaining = int(m.group(1)) if m else None
            try:
                data = json.loads(body)
            except ValueError:
                raise CiError("gh api: unreadable response")
        runs = [{"id": r.get("id"), "name": r.get("name"), "status": r.get("status"),
                 "conclusion": r.get("conclusion"), "url": r.get("html_url"), "attempt": r.get("run_attempt")}
                for r in data.get("workflow_runs", []) if r.get("head_sha", "").lower() == sha.lower()]
        return result(classify(runs), runs=runs, remaining=remaining)

    def rate_limit(self):
        """(remaining, reset epoch) from `gh api rate_limit`, which costs nothing."""
        rc, out, _ = self._gh("rate_limit")
        try:
            core = json.loads(out)["resources"]["core"]
            return int(core["remaining"]), int(core["reset"])
        except (ValueError, KeyError, TypeError):
            return None, None

    def failure_detail(self, repo, sha, res, public=False):
        """(failing jobs, tail of the first failure's log). Called once, when CI first reads failed."""
        failing, tail = [], ""
        for run in res["runs"]:
            if not (run["status"] == "completed" and run["conclusion"] in BAD):
                continue
            try:
                rc, out, _ = self._gh(f"repos/{repo}/actions/runs/{run['id']}/jobs?per_page=100")
                jobs = json.loads(out).get("jobs", []) if rc == 0 else []
            except ValueError:
                jobs = []
            bad = [j for j in jobs if j.get("conclusion") in BAD]
            for j in bad:
                failing.append({"name": f"{run['name']} / {j.get('name')}", "url": j.get("html_url"), "id": j.get("id")})
            if not bad:
                failing.append({"name": run["name"], "url": run["url"], "id": None})
            if not tail and bad and bad[0].get("id") and not public:
                rc, out, _ = self._gh(f"repos/{repo}/actions/jobs/{bad[0]['id']}/logs")
                if rc == 0:
                    tail = "\n".join(out.splitlines()[-40:])
        return failing, tail


class GiteaHost:
    """Gitea combined commit status: GET {api_url}/repos/{repo}/commits/{sha}/status."""
    name = "gitea"

    def __init__(self, api_url, token_file=None, http=_http_json):
        self.api_url, self.token_file, self.http = api_url.rstrip("/"), token_file, http

    def fetch(self, repo, sha, public=False):
        headers = {"Authorization": "token " + _read_token(self.token_file)} if self.token_file else {}
        data, _ = self.http(f"{self.api_url}/repos/{repo}/commits/{sha}/status", headers)
        statuses = data.get("statuses") or []
        runs = [{"id": s.get("id"), "name": s.get("context"), "status": "completed" if s.get("status") != "pending" else "in_progress",
                 "conclusion": {"success": "success", "pending": None}.get(s.get("status"), "failure"),
                 "url": s.get("target_url")} for s in statuses]
        return result(classify(runs), runs=runs)

    def failure_detail(self, repo, sha, res, public=False):
        return [{"name": r["name"], "url": r["url"], "id": None} for r in res["runs"]
                if r["status"] == "completed" and r["conclusion"] in BAD], ""


def make_host(kind: str, cfg: dict, http=_http_json):
    if kind == "github":
        return GitHubHost(http=http)
    if kind == "gitea":
        if not cfg.get("api_url"):
            raise CiError("[ci] kind gitea needs api_url (and token_file, a path) in team.toml")
        return GiteaHost(cfg["api_url"], cfg.get("token_file"), http=http)
    if kind in ("", "none", None):
        raise CiError("no CI host selected: set [ci] kind = \"github\" or \"gitea\" in team.toml (or pass --kind)")
    raise CiError(f"CI host {kind!r} has no CI adapter yet (github and gitea only)")


# ---------------------------------------------------------------- shared cache and lock
class _Lock:
    """Non-blocking exclusive lock on a file: held by the one poller."""

    def __init__(self, path: Path):
        self.path, self.fh, self.held = path, None, False

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(self.path, "a+")
        try:
            if fcntl:
                fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:   # pragma: no cover
                import msvcrt
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
            self.held = True
        except OSError:
            self.held = False
        return self

    def __exit__(self, *exc):
        try:
            if self.held:
                if fcntl:
                    fcntl.flock(self.fh, fcntl.LOCK_UN)
        finally:
            self.fh.close()


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data))
    for attempt in range(8):   # Windows refuses to replace a file another process has open: retry briefly
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.05 * (attempt + 1))
    try:   # the cache is best-effort: a reader holding it open must not fail the wait
        tmp.unlink()
    except OSError:
        pass


def _read(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def default_cache() -> Path:
    return Path(os.environ.get("SWARM_CI_CACHE") or Path.home() / ".cache" / "swarm" / "ci")


class Poller:
    """The shared poll logic. `now` and `sleep` are injectable; tests pass a fake host."""

    def __init__(self, host, cache=None, *, floor=FLOOR_S, backoff=BACKOFF_S, now=time.time, sleep=time.sleep):
        self.host, self.cache = host, Path(cache) if cache else default_cache()
        self.floor, self.backoff, self.now, self.sleep = floor, tuple(backoff), now, sleep
        self.calls = 0

    def _dir(self, repo):
        if not REPO_RE.fullmatch(repo) or ".." in repo.split("/"):
            raise CiError(f"bad --repo {repo!r} (OWNER/REPO)")
        return self.cache / repo

    def _sha(self, sha):
        if not SHA_RE.fullmatch(sha):
            raise CiError(f"bad --sha {sha!r} (a commit SHA, hex)")
        return sha.lower()

    def read(self, repo, sha):
        return _read(self._dir(repo) / f"{self._sha(sha)}.json")

    def record(self, repo, sha, res):
        """Store a final result learned without a host call (a CI event ended the wait) in the shared
        cache, so `status` and every other waiter agree with it. A final result already cached for
        the SHA is kept (a host answer beats an event); a pending one is replaced."""
        sha = self._sha(sha)
        cached = self.read(repo, sha)
        if cached and cached.get("state") in ("green", "failed") and self.fresh(repo, sha, cached):
            return cached
        res = {**res, "repo": repo, "sha": sha, "fetched_at": self.now()}
        try:
            _write(self._dir(repo) / f"{sha}.json", res)
        except OSError:
            pass
        return res

    def want(self, repo, sha):
        _write(self._dir(repo) / f"{self._sha(sha)}.want", {"until": self.now() + WANT_S})

    def _wanted(self, repo, sha):
        d, now, out = self._dir(repo), self.now(), []
        for f in d.glob("*.want"):
            if (_read(f, {}) or {}).get("until", 0) > now:
                out.append(f.stem)
        return out or [sha]

    def interval(self, st) -> float:
        if st.get("slow_until", 0) > self.now() or st.get("remaining") is not None and st["remaining"] < LOW_BUDGET:
            return LOW_BUDGET_S
        streak = st.get("streak", 0)   # consecutive queued/none polls: 60, then 120, then 300
        return self.backoff[min(streak - 1, len(self.backoff) - 1)] if streak else self.floor

    def next_call_at(self, repo) -> float:
        st = _read(self._dir(repo) / "_poll.json", {}) or {}
        return st.get("last_call", 0) + max(self.floor, self.interval(st))

    def fresh(self, repo, sha, cached) -> bool:
        """Only a green is served from cache on its own. A failure is never final by itself: the
        commit may have been re-run since, so poll() asks the host again (at most once per floor)."""
        if not cached or cached.get("state") != "green":
            return False
        return self.now() - cached.get("fetched_at", 0) < TTL_GREEN_S

    def _unverified(self, repo, sha, cached):
        """What a look reports when it could not ask the host for this sha: a cached green stays final;
        a cached failure reads as running, because it may predate a re-run."""
        if cached and cached.get("state") == "failed":
            return result("running", repo=repo, sha=sha, notes=[
                "last CI result for this commit was a failure: re-checking the CI host within the "
                f"{int(self.floor)} s floor"])
        return cached

    def poll(self, repo, sha, force=False):
        """The latest result for repo@sha. Calls the host only when this process wins the repo lock
        AND the floor has elapsed (force: whatever the floor; one call to confirm a CI completion
        event). A cached failure is re-checked, never trusted: until a host call answers for this sha,
        it reads as running. Otherwise returns the shared cache (None if nothing cached yet)."""
        sha = self._sha(sha)
        cached = self.read(repo, sha)
        if self.fresh(repo, sha, cached):
            return cached
        d = self._dir(repo)
        with _Lock(d / ".lock") as lock:
            if not lock.held:
                return self._unverified(repo, sha, cached)   # another process is the poller
            st = _read(d / "_poll.json", {}) or {}
            if not force and self.now() < st.get("last_call", 0) + max(self.floor, self.interval(st)):
                return self._unverified(repo, sha, cached)   # the floor has not elapsed
            target = sha if force else min(self._wanted(repo, sha) or [sha],
                                           key=lambda s: (_read(d / f"{s}.json", {}) or {}).get("fetched_at", 0))
            res = self._call(repo, target, st, prior=cached if target == sha else None)
            _write(d / "_poll.json", st)
            _write(d / f"{target}.json", res)
            if target == sha:
                return self.read(repo, sha)
        return self._unverified(repo, sha, self.read(repo, sha))   # the host was asked about another sha

    def _call(self, repo, sha, st, prior=None):
        now = self.now()
        public = st.get("public_until", 0) > now
        notes = []
        st["last_call"] = now
        self.calls += 1
        try:
            res = self.host.fetch(repo, sha, public=public)
        except RateLimited as exc:
            res = self._limited(repo, sha, st, public, exc)
            notes = res.pop("notes")
        else:
            st["remaining"] = res.get("remaining", st.get("remaining"))
            if st["remaining"] is not None and st["remaining"] < LOW_BUDGET and not public and hasattr(self.host, "rate_limit"):
                rem, reset = self.host.rate_limit()   # free: confirm before slowing everyone down
                if rem is not None:
                    st["remaining"] = rem
        if st.get("remaining") is not None and st["remaining"] < LOW_BUDGET:
            notes.append(f"only {st['remaining']} GitHub API calls left: polling every {LOW_BUDGET_S // 60} min")
        if public or st.get("public_until", 0) > now:
            notes.append("GitHub API rate limit hit: using the unauthenticated public API (60/h per IP)")
        if res["state"] in ("queued", "none"):
            st["streak"] = st.get("streak", 0) + 1
        else:
            st["streak"] = 0
        if prior and prior.get("state") == "failed" and res["state"] in ("green", "failed") \
                and newer_attempt(prior, res):
            # a re-run of the commit is newer than the failure we had: its result is not in yet
            notes.append("CI was re-run after the cached failure: waiting for the new attempt")
            res = result("running", runs=res["runs"], remaining=res.get("remaining"))
        if res["state"] == "failed" and not res["failing"]:
            try:
                res["failing"], res["tail"] = self.host.failure_detail(repo, sha, res, public=public)
            except (CiError, RateLimited, OSError):
                pass
        res.update(repo=repo, sha=sha, fetched_at=now, notes=notes, source="poll")
        return res

    def _limited(self, repo, sha, st, public, exc):
        """A 403: read the (free) rate_limit, then fall back to the public API or slow right down."""
        now = self.now()
        if public:   # the public API refused too: back off hard
            st["slow_until"] = now + LOW_BUDGET_S
            return result("none", notes=["public API rate limit hit too: polling every "
                                         f"{LOW_BUDGET_S // 60} min"])
        rem, reset = self.host.rate_limit() if hasattr(self.host, "rate_limit") else (None, None)
        st["remaining"] = rem if rem is not None else 0
        st["public_until"] = min(reset, now + 3600) if reset and reset > now else now + PUBLIC_MODE_S
        try:
            res = self.host.fetch(repo, sha, public=True)   # same call, now unauthenticated
        except (RateLimited, CiError):
            st["slow_until"] = now + LOW_BUDGET_S
            return result("none", notes=["rate limited on both APIs"])
        res["notes"] = []
        return res


# ---------------------------------------------------------------- events
def event_lookup(ctx, sha, job=None, note=print):
    """A function () -> (kind, text) | None reading CI events for `sha` from the board; None when
    the event core is not installed."""
    try:
        from swarm.board import Board
    except ImportError:
        Board = None
    if Board is None or not hasattr(Board, "events"):
        note("swarm ci: event core not installed (no `swarm event`): polling only", file=sys.stderr)
        return None
    needle = "@" + sha.lower()[:40]
    short = sha.lower()

    def look():
        with ctx.open_board() as board:
            jobs = [job] if job else [j.job for j in board.jobs() if getattr(j, "status", "") == "active"]
            for j in jobs:
                for e in board.events(j):
                    if e.kind in CI_EVENT_KINDS and (needle in (e.key or "") or needle in (e.text or "")
                                                     or any(short.startswith(m) or m.startswith(short)
                                                            for m in re.findall(r"@([0-9a-f]{7,40})", (e.key or "").lower()))):
                        return e.kind, e.text
        return None
    return look


def event_state(kind):
    return "failed" if kind == "CI-FAILED" or kind.endswith("FAILED") else "green"


# ---------------------------------------------------------------- commands
def render(res) -> list[str]:
    lines = [f"CI {res['state']} for {res.get('repo', '?')}@{str(res.get('sha', ''))[:12]}"
             + (f" (from event {res['event']})" if res.get("event") else "")]
    for run in res.get("runs", []):
        lines.append(f"  {run.get('name')}: {run.get('conclusion') or run.get('status')}")
    if res["state"] == "failed":
        for f in res.get("failing", []):
            lines.append(f"  FAILED: {f['name']}" + (f" {f['url']}" if f.get("url") else ""))
        if res.get("tail"):
            lines += ["  --- tail of the first failure ---", *("  " + t for t in res["tail"].splitlines())]
    for note in res.get("notes", []):
        lines.append("  note: " + note)
    return lines


def emit(res, as_json, out=None):
    out = out or sys.stdout
    if as_json:
        print(json.dumps(res), file=out)
    else:
        print("\n".join(render(res)), file=out)


def _event_result(poller, repo, sha, events):
    """A final result from a CI event for repo@sha (recorded in the cache), else None."""
    hit = events() if events else None
    if not hit:
        return None
    kind, text = hit
    res = result(event_state(kind), repo=repo, sha=poller._sha(sha), event=kind, notes=[text], source="event")
    poller.record(repo, sha, res)
    return res


def run_status(poller, repo, sha, as_json=False, events=None) -> int:
    res = poller.poll(repo, sha)
    if not (res and res["state"] in ("green", "failed")):   # what ended a wait ends a status look too
        res = _event_result(poller, repo, sha, events) or res
    res = res or result("none", repo=repo, sha=sha,
                        notes=["no cached result yet; another process is polling or the floor has not elapsed"])
    res = {**res, "source": "cache" if res.get("source") == "poll" and poller.calls == 0 else res.get("source", "cache")}
    emit(res, as_json)
    return 0


def run_wait(poller, repo, sha, timeout, as_json=False, events=None, say=None) -> int:
    say = say or (lambda m: print(m, file=sys.stderr))
    deadline = poller.now() + timeout
    announced = set()
    sha = poller._sha(sha)
    while True:
        res = _event_result(poller, repo, sha, events)
        if res:   # recorded in the shared cache: `status` now says the same
            emit(res, as_json)
            return 1 if res["state"] == "failed" else 0
        poller.want(repo, sha)
        res = poller.poll(repo, sha)
        if res and res["state"] in ("green", "failed"):
            emit(res, as_json)
            return 0 if res["state"] == "green" else 1
        for n in (res or {}).get("notes", []):
            if n not in announced:
                announced.add(n)
                say("swarm ci: " + n)
        if poller.now() >= deadline:
            emit({**(res or result("none", repo=repo, sha=sha)), "timeout": True}, as_json)
            say(f"swarm ci: timed out after {int(timeout)}s waiting for CI on {sha[:12]}")
            return 124
        poller.sleep(min(TICK_S, poller.floor, max(0.0, deadline - poller.now())))


def confirmer(repo, poller):
    """confirm(sha) for the event sources: "green"/"failed" when the whole commit's CI is final
    (one call through the shared poller, its cache when that is already final), else its state."""
    def confirm(sha):
        res = poller.read(repo, sha)
        if not (res and res.get("state") in ("green", "failed") and poller.fresh(repo, sha, res)
                and res.get("source") != "event"):
            res = poller.poll(repo, sha, force=True)
        return (res or {}).get("state")
    return confirm


def ci_settings(ctx, override=None) -> tuple[str, dict]:
    import tomllib
    path = Path(os.environ.get("SWARM_TEAM_CONFIG") or ctx.config_dir / "team.toml").expanduser()
    cfg = {}
    if path.is_file():
        try:
            with path.open("rb") as fh:
                cfg = tomllib.load(fh).get("ci", {})
        except (OSError, ValueError) as exc:
            raise CiError(f"team.toml unreadable: {exc}")
    return override or cfg.get("kind") or "none", cfg if isinstance(cfg, dict) else {}


def setup_ci(parser) -> None:
    parser.description = ("Budget-safe CI state for an exact SHA, shared by every agent on the box (one poller, one "
                          "cache, one call per repo per 60 s, events first). Use this instead of `gh run watch`.")
    sub = parser.add_subparsers(dest="ci_cmd", metavar="status|wait")
    for name, text in (("status", "print the CI state of one SHA: none, queued, running, green or failed"),
                       ("wait", "block until CI on the exact SHA completes (exit 0 green, 1 failed, 124 timeout)")):
        p = sub.add_parser(name, help=text, description=text)
        p.add_argument("--repo", required=True, help="OWNER/REPO")
        p.add_argument("--sha", required=True, help="the exact head commit SHA")
        p.add_argument("--kind", help="override [ci] kind (github|gitea)")
        p.add_argument("--json", action="store_true", dest="as_json")
        if name == "wait":
            p.add_argument("--timeout", default="90m", help="give up after this long (default 90m)")
            p.add_argument("--job", help="board job whose CI events end the wait early (default: active jobs)")


def run_ci(ctx, args, *, host=None, poller=None) -> int:
    if not getattr(args, "ci_cmd", None):
        print("swarm ci: use `swarm ci status` or `swarm ci wait` (see --help)", file=sys.stderr)
        return 2
    try:
        if poller is None:
            if host is None:
                kind, cfg = ci_settings(ctx, args.kind)
                host = make_host(kind, cfg)
            poller = Poller(host)
        if args.ci_cmd == "status":
            events = event_lookup(ctx, args.sha, os.environ.get("SWARM_JOB") or None, note=lambda *a, **k: None)
            return run_status(poller, args.repo, args.sha, args.as_json, events)
        events = event_lookup(ctx, args.sha, args.job or os.environ.get("SWARM_JOB") or None)
        return run_wait(poller, args.repo, args.sha, parse_duration(args.timeout), args.as_json, events)
    except CiError as exc:
        print(f"swarm ci: {exc}", file=sys.stderr)
        return 2
