---
name: ci
description: Use when an agent must know or wait for the CI result of an exact commit, or when a project needs CI and pull-request events (GitHub or Gitea) delivered to a Swarm job. Never use `gh run watch`.
---

# ci

Everything that talks to a CI or repo host lives here. Core Swarm knows nothing about code, PRs or CI;
the engineering-team skill keeps only the process and uses this skill through its CLI and its events.

## Wait for CI

Wait for CI ONLY with `swarm ci wait --repo OWNER/REPO --sha <exact head> [--timeout 90m]`
(exit 0 green, 1 failed with the failing jobs and a log tail, 124 timeout). `swarm ci status` is a
one-off look. Never `gh run watch`, never a `gh run list` loop: they exhaust the shared API budget.

One cached poller per box serves every agent: at most one host call per repo per 60 s, backoff
60/120/300 s on queued runs, 10 minutes under 500 calls left, a public-API fallback on 403, and a CI
event for the exact SHA ends the wait with no call at all.

## Configure

The host comes from `[ci] kind = "github" | "gitea"` in `team.toml` (Gitea also needs `api_url` and
`token_file`, a path). Nothing is inferred. An old-style section is rewritten to `[ci]` once by
`swarm upgrade` or `swarm init`, with a `.bak` next to it.

## Events

GitHub webhooks come first. The `github` source takes pushed `workflow_run`, `check_suite`, `pull_request`,
`pull_request_review` and `issue_comment` payloads (HMAC `X-Hub-Signature-256`, checked on the raw body) and
raises `NEEDS-REVIEW`, `REVIEW-CHANGES`, `CI-FAILED`, `CI-GREEN` and `READY-TO-LAND` with no API call,
from the payload plus remembered state. Deliver them with `gh webhook forward` (the listener supervises it
when `forward = true`) or a normal repo webhook when the box is public. The `gitea` source verifies HMAC
webhooks and polls commit status as a fallback, since Gitea sends no CI webhooks. Board posts
`BRANCH READY <branch> <sha>` become `BRANCH-READY`. Polling never runs faster than 600 s.

Config, in the swarm `config.toml` (paths only, never secret values):

    [ci.github]            # or [ci.gitea]; active only when `repo` is set
    repo = "owner/name"
    secret_file = "~/.config/me/webhook-secret"
    token_file = "~/.config/me/ci-token"
    forward = true         # github: run `gh webhook forward` (no secret on its command line: it
                           # delivers to a local route only this OS user's processes may use; Linux)
    api_url = "..."        # gitea; github defaults to api.github.com
    job = "my-job"         # optional: default every active job

## Layout

- `swarm_plugin.py` registers `swarm ci` and the event sources.
- `ci_wait.py` is the shared poller and the `status`/`wait` commands.
- `ci_events.py` holds the GitHub and Gitea adapters, the webhook state machine and BRANCH-READY.
