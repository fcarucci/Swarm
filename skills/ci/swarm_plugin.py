"""ci plugin: everything that talks to a CI or repo host.

Commands: `swarm ci status|wait` (one shared, budget-safe poller per box). Event sources: GitHub
(pushed webhooks first, `gh webhook forward` as a supervised helper), Gitea (HMAC webhooks plus a
commit-status poll as a fallback) and BRANCH-READY board posts. Config lives in the swarm
`config.toml` under `[ci.github]` and `[ci.gitea]` (paths to secrets, never values). The
engineering-team plugin only consumes the CLI and the events and imports nothing from this directory.
"""
from __future__ import annotations

import importlib.util
import sys
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _ci_wait():
    """The sibling module, loaded by path: a plugin file is not imported as part of a package."""
    name = "swarm_ci_wait"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, HERE / "ci_wait.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return sys.modules[name]


def _ci_events():
    name = "swarm_ci_events"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, HERE / "ci_events.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return sys.modules[name]


def _config(config_dir: Path) -> dict:
    try:
        with (Path(config_dir) / "config.toml").open("rb") as stream:
            data = tomllib.load(stream)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _section(config_dir: Path, kind: str) -> dict:
    """`[ci.<kind>]` of the swarm config; empty (source inactive) when absent."""
    ci = _config(config_dir).get("ci", {})
    section = ci.get(kind, {}) if isinstance(ci, dict) else {}
    return section if isinstance(section, dict) else {}


def _jobs(ctx, section) -> list[str]:
    """The job(s) events go to: `job` in the source config, else every active job on the board."""
    if section.get("job"):
        return [str(section["job"])]
    return [j.job for j in ctx.board.jobs() if j.status == "active"]


def _local_token():
    """The running listener's local-route token (swarm.events_listener.local_token), else None."""
    try:
        from swarm.events_listener import local_token
    except ImportError:
        return None
    try:
        return local_token()
    except Exception:
        return None


def _confirm(config_dir: Path, section: dict):
    """confirm(sha) through the shared `swarm ci` poller (one call per completion, the box's cache
    and budget); None-returning when the CI host can't be set up."""
    cw = _ci_wait()
    try:
        poller = cw.Poller(cw.make_host("github", {}))
    except Exception:
        return lambda sha: None
    return cw.confirmer(str(section.get("repo") or ""), poller)


def ci_source(kind: str, config_dir: Path):
    """Event-source callbacks for one CI host (gitea|github). Active only when `[ci.<kind>]` with a
    `repo` exists in config.toml; verify() runs before any context exists, so it reads the file itself."""
    fe = _ci_events()
    push_state = fe.PushState(Path(config_dir) / f"ci-{kind}-state.json")

    def make(cfg):
        return fe.CI_HOSTS[kind](cfg) if cfg.get("repo") else None

    def verify(headers, body) -> bool:
        try:
            host = make(_section(config_dir, kind))
            return bool(host and host.verify(headers, body))
        except (OSError, ValueError, KeyError):
            return False   # a missing secret file or a bad config never accepts a request

    def handle(headers, body, ctx):
        cfg = _section(config_dir, kind)
        host = make(cfg)
        if host is None:
            return []
        if kind == "github":   # callbacks first: pushed payloads plus remembered state; green is confirmed
            confirm = _confirm(config_dir, cfg)
            return [e for job in _jobs(ctx, cfg) for e in fe.handle_push(push_state, job, headers, body, confirm)]
        return [e for job in _jobs(ctx, cfg) for e in fe.handle_webhook(host, job, headers, body)]

    def poll(ctx):
        cfg = _section(config_dir, kind)
        host = make(cfg)
        if host is None:
            return None
        out = []
        if kind == "github" and push_state.unconfirmed:
            confirm = _confirm(config_dir, cfg)
            out += [e for job in _jobs(ctx, cfg) for e in fe.confirm_pending(push_state, job, confirm)]
        return out + [e for job in _jobs(ctx, cfg) for e in fe.poll_prs(host, job)]

    def helpers(_cfg):
        """`gh webhook forward` for the supervisor to keep running when `forward = true`."""
        cfg = _section(config_dir, kind)
        if kind != "github" or not cfg.get("forward") or not cfg.get("repo"):
            return []
        token = _local_token()
        if not token:   # no listener token (or an old core): no forwarder rather than an open route
            return []
        try:
            port = _config(config_dir).get("events", {}).get("port", 8923)
            argv = fe.forward_argv(cfg, port, token=token)
        except (OSError, ValueError, KeyError):
            return []
        return [{"name": "forward", "argv": argv}]

    return verify, handle, poll, helpers


def branch_ready_poll(ctx):
    """BRANCH-READY events from `BRANCH READY ...` board posts (no model scans the board)."""
    fe = _ci_events()
    out = []
    for job in _jobs(ctx, {}):
        out += fe.branch_ready_specs(job, [m.message for m in ctx.board.recent_messages(100, job)])
    return out


def register_event_sources(api) -> None:
    """Register through core's event-source interface when this swarm has it."""
    register = getattr(api, "register_event_source", None)
    if register is None:
        return
    for kind, routes in (("gitea", ["/gitea"]), ("github", ["/github"])):
        verify, handle, poll, helpers = ci_source(kind, api.config_dir)
        interval = _ci_events().poll_interval(_section(api.config_dir, kind))
        # github's forwarder delivers unsigned to a local route (no secret in its argv); a core
        # without local routes gets no forwarder at all rather than a secret on a command line
        extra = {"helpers": helpers, "local_routes": [_ci_events().FORWARD_ROUTE]} if kind == "github" else {}
        try:   # polling is only a fallback (the API budget is shared): never faster than 10 minutes
            register(kind, routes=routes, verify=verify, handle=handle, poll=poll, poll_interval_s=interval, **extra)
        except TypeError:   # a core without helper or local-route support
            register(kind, routes=routes, verify=verify, handle=handle, poll=poll, poll_interval_s=interval)
    register("branch-ready", routes=["/branch-ready"], verify=lambda headers, body: False,
             handle=lambda headers, body, ctx: [], poll=branch_ready_poll, poll_interval_s=60)


def run_ci(ctx, args) -> int:
    return _ci_wait().run_ci(ctx, args)


def setup_ci(parser) -> None:
    _ci_wait().setup_ci(parser)


def register(api) -> None:
    api.add_command("ci", run_ci, setup=setup_ci,
                    help="budget-safe CI status/wait for an exact SHA: one shared poller per box (use instead of gh run watch)")
    register_event_sources(api)
