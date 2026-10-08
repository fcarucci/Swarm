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

## Layout

- `swarm_plugin.py` registers `swarm ci`.
- `ci_wait.py` is the shared poller and the `status`/`wait` commands.
- Host adapters (GitHub webhooks first, Gitea), webhook event sources and BRANCH-READY arrive from
  the adapters branch, next to these files.
