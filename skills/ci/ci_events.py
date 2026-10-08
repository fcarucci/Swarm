"""CI event sources of the ci plugin: GitHub (callbacks first), Gitea (polled status) and BRANCH-READY board posts.

Core swarm never interprets these kinds. Each source maps CI host facts to event specs
(`dict(job, kind, key, text, to)`), addressed to the PM (`@pm`), keyed `kind:N@sha`, so a
duplicate is a no-op in `swarm event post`:

  NEEDS-REVIEW N@sha     a PR opened, or a new head, and no review verdict names this head
  REVIEW-CHANGES N@sha   the latest verdict for this head is CHANGES REQUIRED
  CI-FAILED N@sha ctx..  CI on the current head failed (failing contexts listed)
  READY-TO-LAND N@sha    verdict VERIFIED and CI success agree on the EXACT current head sha
  CI-GREEN sha           every recorded CI run of a commit succeeded (ends `swarm ci wait`, no API call)
  BRANCH-READY b@sha     a board post `BRANCH READY <branch> <sha> ...`

Verdicts are PR comments / reviews whose first line is `REVIEW #N @ <40 hex>: VERIFIED|CHANGES
REQUIRED`. State is never kept here: every webhook or poll re-reads the CI host for the PR, so a
restart loses nothing. Secrets are read from `secret_file` / `token_file` paths only; the config
holds paths, never values, and nothing here logs a body or a token.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import urllib.request
from pathlib import Path

PM = "@pm"
VERDICT_RE = re.compile(r"REVIEW #(\d+) @ ([0-9a-f]{40}): (VERIFIED|CHANGES REQUIRED)")
BRANCH_READY_RE = re.compile(r"BRANCH READY[:\s]+(\S+?)\s*[@\s]\s*([0-9a-f]{7,40})\b")
SHA_RE = re.compile(r"[0-9a-f]{40}")
NEW_HEAD_ACTIONS = ("opened", "synchronize", "synchronized", "reopened", "ready_for_review")


def read_secret(path) -> bytes:
    """The content of a secret file; the config only ever names the path."""
    return Path(str(path)).expanduser().read_bytes().strip()


def spec(job, kind, n, sha, text="", to=PM):
    return {"job": job, "kind": kind, "key": f"{kind}:{n}@{sha}", "to": to,
            "text": f"{kind} {n}@{sha} {text}".strip()[:500]}


def verdict_for(comments, n, sha):
    """(verdict, rest of the first line) of the LATEST verdict naming `sha`, else (None, '')."""
    found = (None, "")
    for body in comments:
        first = (body or "").lstrip().splitlines()[:1]
        m = VERDICT_RE.match(first[0]) if first else None
        if m and int(m.group(1)) == n and m.group(2) == sha:
            found = (m.group(3), first[0][len(m.group(0)):].strip(" :-")[:150])
    return found


def decide(job, n, sha, title, comments, ci, *, new_head=True):
    """Events for one PR at its current head. `ci` is (state, failing text) with state one of
    success, failure, pending. READY-TO-LAND needs verdict AND CI to agree on this exact sha."""
    out = []
    verdict, detail = verdict_for(comments, n, sha)
    state, failing = ci
    if verdict is None and new_head:
        out.append(spec(job, "NEEDS-REVIEW", n, sha, title[:80]))
    if verdict == "CHANGES REQUIRED":
        out.append(spec(job, "REVIEW-CHANGES", n, sha, detail))
    if state == "failure":
        out.append(spec(job, "CI-FAILED", n, sha, failing[:200]))
    if state == "success" and verdict == "VERIFIED":
        out.append(spec(job, "READY-TO-LAND", n, sha))
    return out


def ci_events(job, sha, ci, *, has_pr):
    """Commit-level CI events straight from pushed payloads: CI-GREEN when every recorded run of
    `sha` succeeded, and CI-FAILED when it failed and no PR is known to carry it (a PR gets the
    PR-keyed CI-FAILED from `decide`)."""
    state, failing = ci
    if state == "success":
        return [spec(job, "CI-GREEN", "", sha)]
    if state == "failure" and not has_pr:
        return [spec(job, "CI-FAILED", "", sha, failing[:200])]
    return []


def _json(req, timeout=20):
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


class CiHost:
    """One CI host's API: open PRs, a PR's comments, the CI state of a commit."""
    kind = ""
    sig_headers: tuple = ()

    def __init__(self, section: dict):
        self.api = str(section.get("api_url") or self.default_api).rstrip("/")
        self.repo = str(section.get("repo") or "")
        self.token_file = section.get("token_file")
        self.secret_file = section.get("secret_file")

    default_api = ""

    def get(self, path):
        headers = {"Accept": "application/json"}
        if self.token_file:
            headers["Authorization"] = self.auth(read_secret(self.token_file).decode())
        return _json(urllib.request.Request(self.api + path, headers=headers))

    def verify(self, headers, body: bytes) -> bool:
        if not self.secret_file:
            return False
        want = hmac.new(read_secret(self.secret_file), body, hashlib.sha256).hexdigest()
        h = {k.lower(): v for k, v in headers.items()}
        for name in self.sig_headers:
            got = h.get(name, "")
            if got.startswith("sha256="):
                got = got[7:]
            if got and hmac.compare_digest(got, want):
                return True
        return False


class Gitea(CiHost):
    kind = "gitea"
    sig_headers = ("x-gitea-signature", "x-hub-signature-256")

    def auth(self, token):
        return "token " + token

    def open_prs(self):
        return [(p["number"], p["head"]["sha"], p.get("title", ""))
                for p in self.get(f"/repos/{self.repo}/pulls?state=open&limit=50")]

    def comments(self, n):
        return [c.get("body", "") for c in self.get(f"/repos/{self.repo}/issues/{n}/comments?limit=50")]

    def ci(self, sha):
        st = self.get(f"/repos/{self.repo}/commits/{sha}/status")
        statuses = st.get("statuses") or []
        state = st.get("state")
        if not statuses or state not in ("success", "failure", "error"):
            return "pending", ""
        if state == "success":
            return "success", ""
        bad = " ".join(f"{x['context']}:{x['status']}" for x in statuses if x["status"] != "success")
        return "failure", bad

    def parse(self, headers, payload):
        """[(n, sha|None)] pull requests a webhook may have changed, plus whether the head is new."""
        h = {k.lower(): v for k, v in headers.items()}
        ev = h.get("x-gitea-event", "")
        if ev == "pull_request":
            pr = payload.get("pull_request", {})
            if payload.get("action") in NEW_HEAD_ACTIONS:
                return [(pr.get("number"), pr.get("head", {}).get("sha"), pr.get("title", ""), True)]
            return []
        if ev == "issue_comment" and payload.get("action") == "created" and payload.get("is_pull"):
            if (payload.get("comment", {}).get("body") or "").startswith("REVIEW #"):
                return [(payload["issue"]["number"], None, payload["issue"].get("title", ""), False)]
            return []
        if ev == "status" and payload.get("state") in ("success", "failure", "error"):
            return [(None, payload.get("sha"), "", False)]
        return []


class GitHub(CiHost):
    kind = "github"
    sig_headers = ("x-hub-signature-256",)
    default_api = "https://api.github.com"

    def auth(self, token):
        return "Bearer " + token

    def open_prs(self):
        return [(p["number"], p["head"]["sha"], p.get("title", ""))
                for p in self.get(f"/repos/{self.repo}/pulls?state=open&per_page=50")]

    def comments(self, n):
        out = [c.get("body", "") for c in self.get(f"/repos/{self.repo}/issues/{n}/comments?per_page=50")]
        out += [r.get("body", "") for r in self.get(f"/repos/{self.repo}/pulls/{n}/reviews?per_page=50")]
        return out

    def ci(self, sha):
        runs = self.get(f"/repos/{self.repo}/commits/{sha}/check-runs?per_page=100").get("check_runs", [])
        st = self.get(f"/repos/{self.repo}/commits/{sha}/status")
        statuses = st.get("statuses") or []
        bad = [f"{r['name']}:{r.get('conclusion')}" for r in runs
               if r.get("status") == "completed"
               and r.get("conclusion") not in ("success", "skipped", "neutral")]
        bad += [f"{s['context']}:{s['state']}" for s in statuses if s["state"] in ("failure", "error")]
        if bad:
            return "failure", " ".join(bad)
        if not runs and not statuses:
            return "pending", ""
        if any(r.get("status") != "completed" for r in runs) or any(s["state"] == "pending" for s in statuses):
            return "pending", ""
        return "success", ""

    def parse(self, headers, payload):
        h = {k.lower(): v for k, v in headers.items()}
        ev = h.get("x-github-event", "")
        if ev == "pull_request":
            pr = payload.get("pull_request", {})
            if payload.get("action") in NEW_HEAD_ACTIONS:
                return [(pr.get("number"), pr.get("head", {}).get("sha"), pr.get("title", ""), True)]
            return []
        if ev in ("pull_request_review", "issue_comment"):
            if payload.get("action") not in ("submitted", "created"):
                return []
            body = (payload.get("review") or payload.get("comment") or {}).get("body") or ""
            n = (payload.get("pull_request") or payload.get("issue") or {}).get("number")
            if body.startswith("REVIEW #") and n:
                return [(n, None, "", False)]
            return []
        if ev == "check_suite" and payload.get("action") == "completed":
            suite = payload.get("check_suite", {})
            prs = [(p.get("number"), suite.get("head_sha"), "", False) for p in suite.get("pull_requests", [])]
            return prs or [(None, suite.get("head_sha"), "", False)]
        if ev == "status" and payload.get("state") in ("success", "failure", "error"):
            return [(None, payload.get("sha"), "", False)]
        return []


CI_HOSTS = {"gitea": Gitea, "github": GitHub}
MIN_POLL_S = 600   # polling is a fallback only: never faster than this (the API budget is shared)


def poll_interval(section) -> int:
    try:
        return max(MIN_POLL_S, int(section.get("poll_interval_s", MIN_POLL_S)))
    except (TypeError, ValueError):
        return MIN_POLL_S


class PushState:
    """What pushed GitHub webhooks have told us, so READY-TO-LAND and CI-FAILED need no API call.
    Kept in memory and mirrored to a JSON file (paths/ids/shas only, no secrets and no bodies)."""

    def __init__(self, path=None):
        self.path = Path(path) if path else None
        self.heads, self.verdicts, self.ci, self.titles = {}, {}, {}, {}
        if self.path and self.path.is_file():
            try:
                d = json.loads(self.path.read_text())
                self.heads = {int(k): v for k, v in d.get("heads", {}).items()}
                self.verdicts = {int(k): v for k, v in d.get("verdicts", {}).items()}
                self.ci = d.get("ci", {})
                self.titles = {int(k): v for k, v in d.get("titles", {}).items()}
            except (OSError, ValueError):
                pass

    def save(self):
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"heads": self.heads, "verdicts": self.verdicts,
                                       "ci": self.ci, "titles": self.titles}))
            tmp.replace(self.path)
        except OSError:
            pass

    def ci_state(self, sha):
        runs = self.ci.get(sha) or {}
        bad = [f"{k}:{v}" for k, v in runs.items() if v not in ("success", "skipped", "neutral")]
        if bad:
            return "failure", " ".join(bad)
        return ("success", "") if runs else ("pending", "")


def handle_push(state, job, headers, body: bytes):
    """GitHub webhook -> events with NO API call, from `state` plus the payload alone."""
    try:
        p = json.loads(body or b"{}")
    except ValueError:
        return []
    ev = {k.lower(): v for k, v in headers.items()}.get("x-github-event", "")
    touched, new_head, sha_events = [], False, []
    if ev == "pull_request" and p.get("action") in NEW_HEAD_ACTIONS:
        pr = p.get("pull_request", {})
        n, sha = pr.get("number"), pr.get("head", {}).get("sha")
        if n and sha:
            state.heads[n], state.titles[n] = sha, pr.get("title", "")
            touched, new_head = [n], True
    elif ev in ("pull_request_review", "issue_comment") and p.get("action") in ("submitted", "created"):
        body_text = (p.get("review") or p.get("comment") or {}).get("body") or ""
        m = VERDICT_RE.match(body_text.lstrip().splitlines()[0]) if body_text.strip() else None
        if m:
            n = int(m.group(1))
            state.verdicts.setdefault(n, []).append(body_text.lstrip().splitlines()[0])
            state.verdicts[n] = state.verdicts[n][-20:]
            state.heads.setdefault(n, m.group(2))
            touched = [n]
    elif ev in ("workflow_run", "check_suite") and p.get("action") == "completed":
        run = p.get("workflow_run") or p.get("check_suite") or {}
        sha, concl = run.get("head_sha"), run.get("conclusion")
        if sha and concl:
            state.ci.setdefault(sha, {})[str(run.get("name") or ev)] = concl
            listed = [q.get("number") for q in run.get("pull_requests") or [] if q.get("number")]
            for n in listed:   # the run itself names its PR: its head is known without any API call
                state.heads.setdefault(n, sha)
            touched = listed + [n for n, h in state.heads.items() if h == sha and n not in listed]
            sha_events = ci_events(job, sha, state.ci_state(sha), has_pr=bool(touched))
    out = list(sha_events)
    for n in touched:
        sha = state.heads.get(n)
        if sha:
            out += decide(job, n, sha, state.titles.get(n, ""), state.verdicts.get(n, []),
                          state.ci_state(sha), new_head=new_head)
    state.save()
    return out


def forward_argv(section, port, route="/github"):
    """argv of `gh webhook forward` (cli/gh-webhook): GitHub pushes events over an outbound websocket
    to the local listener, so a box with no public endpoint still gets callbacks. The secret is read
    from `secret_file` at launch; the config never holds the value. For the supervisor to run when
    `[ci.github] forward = true`."""
    events = section.get("forward_events") or "workflow_run,check_suite,pull_request,pull_request_review,issue_comment"
    argv = ["gh", "webhook", "forward", f"--repo={section['repo']}", f"--events={events}",
            f"--url=http://127.0.0.1:{int(port)}{route}"]
    if section.get("secret_file"):
        argv += ["--secret", read_secret(section["secret_file"]).decode()]
    return argv


def evaluate(ci, job, n, sha=None, title="", new_head=False):
    """Re-read the CI host for PR n at its CURRENT head and return its events. A verdict or CI
    result for an older head than the PR's current one yields nothing."""
    head = sha
    if head is None or not new_head:
        match = next((p for p in ci.open_prs() if p[0] == n), None)
        if match is None:
            return []
        head, title = match[1], match[2] or title
    if sha and not new_head and sha != head:
        return []
    return decide(job, n, head, title, ci.comments(n), ci.ci(head), new_head=new_head)


def handle_webhook(ci, job, headers, body: bytes):
    try:
        payload = json.loads(body or b"{}")
    except ValueError:
        return []
    out = []
    for n, sha, title, new_head in ci.parse(headers, payload):
        if n is None:   # a status/CI result for a commit: find the PR whose head it is
            for pn, psha, ptitle in ci.open_prs():
                if psha == sha:
                    out += evaluate(ci, job, pn, psha, ptitle)
            continue
        out += evaluate(ci, job, n, sha, title, new_head=new_head)
    return out


def poll_prs(ci, job):
    """Gitea sends no CI webhooks: re-evaluate every open PR at its current head."""
    out = []
    for n, sha, title in ci.open_prs():
        out += evaluate(ci, job, n, sha, title, new_head=True)
    return out


def branch_ready_specs(job, messages, to=PM):
    """BRANCH-READY specs from board posts `BRANCH READY <branch> <sha> ...`."""
    out = []
    for text in messages:
        m = BRANCH_READY_RE.search(text or "")
        if m:
            branch, sha = m.group(1), m.group(2)
            out.append({"job": job, "kind": "BRANCH-READY", "key": f"BRANCH-READY:{branch}@{sha}",
                        "to": to, "text": f"BRANCH-READY {branch}@{sha} {text.strip()[:300]}"[:500]})
    return out
