"""Command plugins: extensions of the swarm CLI that live outside the core.

A plugin is a Python module with a `register(api)` function. Core swarm discovers plugins when it
parses a command line, and for blocker expiry/orchestrator hooks on an active job; it works
with none installed. Where it looks, in order
(a name loaded earlier wins; the rest are reported by `swarm plugins`):

  1. a plugins directory next to the swarm config: <config dir>/plugins/<name>.py (or <name>/__init__.py),
     and every directory in $SWARM_PLUGIN_PATH (os.pathsep-separated), same layout;
  2. plugins shipped inside the swarm plugin's skills: <plugin root>/skills/<skill>/swarm_plugin.py;
  3. Python entry points in the group "swarm.plugins" (each names a module or a `register` callable).

`[plugins] disabled = ["name", ...]` in the swarm config skips plugins by name.

A plugin that fails to import or to register is recorded (shown by `swarm plugins`; a core command
prints nothing about it) and skipped: the core commands keep working. A plugin command or hook that raises is
reported as one line on stderr and exit status 1 (a command) or ignored with a warning (a hook).

The API handed to register(api) is deliberately small and stable (see docs/PLUGINS.md):

    api.name                      the plugin's name
    api.config_dir                the directory of the swarm config (a pathlib.Path)
    api.add_command(name, run, setup=None, help=None)
    api.extend_command(name, setup=None, before=None, after=None)
    api.add_status_lines(fn)
    api.add_pipeline_recipe(fn)
    api.register_event_source(name, routes=, verify=, handle=, poll=None, poll_interval_s=None, helpers=None)

run(ctx, args) -> int | None is a new command; setup(parser) adds its arguments. extend_command
adds arguments to an existing core command and hooks around it: before(ctx, args) -> int | None
runs before the command (a non-zero status stops it: that is how a plugin rejects an argument) and
after(ctx, args) runs once the command succeeded. add_status_lines(fn): fn(ctx, job) -> list[str]
gives lines `swarm status --job J` prints after the job's own details. ctx is a PluginContext.
"""
from __future__ import annotations

import dataclasses
import importlib
import importlib.util
import os
import stat
import sys
from pathlib import Path
from typing import Callable

from . import paths

API_VERSION = 1
ENTRY_POINT_GROUP = "swarm.plugins"
SHIPPED_FILE = "swarm_plugin.py"


def _check_file_trust(path: Path) -> None:
    """Cheap POSIX checks, not a sandbox: same-user writers remain trusted (docs/PLUGINS.md)."""
    if os.name == "nt":
        return
    st = path.lstat()
    if stat.S_ISLNK(st.st_mode):
        raise ValueError("refused: symlink")
    if st.st_uid != os.getuid():
        raise ValueError("refused: owned by another user")
    if st.st_mode & 0o002:
        raise ValueError("refused: world-writable")
    if st.st_mode & 0o020 and st.st_gid != os.getgid():
        raise ValueError("refused: writable by another group")


@dataclasses.dataclass
class PluginInfo:
    name: str
    source: str                    # a file path, or "entry point <ep>"
    commands: list[str] = dataclasses.field(default_factory=list)
    extends: list[str] = dataclasses.field(default_factory=list)
    sources: list[str] = dataclasses.field(default_factory=list)   # event sources it registered
    error: str | None = None       # why it isn't loaded
    disabled: bool = False         # skipped on purpose ([plugins] disabled): not an error

    @property
    def loaded(self) -> bool:
        return self.error is None


class PluginContext:
    """What a command or hook of a plugin gets: the loaded config, and the board on demand."""

    def __init__(self, cfg: dict, config_path: Path, plugin: str, board=None, registry=None):
        self.cfg, self.config_path, self.plugin = cfg, Path(config_path), plugin
        self.config_dir = self.config_path.parent
        self._board = board
        self.registry = registry

    def open_board(self):
        """A board context manager with the registry attached for durable blocker events."""
        import contextlib
        from swarm.board import open_board
        @contextlib.contextmanager
        def opened():
            if self._board is not None:
                yield self._board
            else:
                with open_board(self.cfg) as board:
                    board.plugin_registry = self.registry
                    yield board
        return opened()

    def job_data(self, board, job: str) -> dict[str, str]:
        """This plugin's settings of `job` (Board.job_data keys it kept, without the prefix)."""
        prefix = self.plugin + "."
        return {k[len(prefix):]: v for k, v in board.job_data(job).items() if k.startswith(prefix)}

    def set_job_data(self, board, job: str, key: str, value: str | None) -> bool:
        """Keep `key` = `value` (None removes it) with the job, under this plugin's own prefix."""
        return board.set_job_data(job, f"{self.plugin}.{key}", value)


@dataclasses.dataclass
class EventSource:
    """An external event source a plugin registered (`swarm events serve` serves it)."""
    plugin: str
    name: str
    routes: tuple
    verify: Callable
    handle: Callable
    poll: Callable | None = None
    poll_interval_s: int | None = None
    helpers: Callable | None = None   # helpers(config) -> list[dict(name, argv, env=None)]


@dataclasses.dataclass
class _Command:
    plugin: str
    name: str
    run: Callable
    setup: Callable | None
    help: str | None


@dataclasses.dataclass
class _Extension:
    plugin: str
    command: str
    setup: Callable | None
    before: Callable | None
    after: Callable | None


class PluginAPI:
    """The object a plugin's register() receives."""

    def __init__(self, registry: "Registry", name: str, config_dir: Path):
        self._r, self.name, self.config_dir = registry, name, config_dir
        self.api_version = API_VERSION

    def add_command(self, name: str, run: Callable, setup: Callable | None = None,
                    help: str | None = None) -> None:
        if not isinstance(name, str) or not name.replace("-", "").isalnum() or not name[:1].isalpha():
            raise ValueError(f"bad command name {name!r}")
        if not callable(run):
            raise TypeError("run must be callable")
        if name in self._r.core_commands or name in self._r.commands:
            owner = self._r.commands[name].plugin if name in self._r.commands else "swarm core"
            raise ValueError(f"command {name!r} already exists ({owner})")
        self._r.commands[name] = _Command(self.name, name, run, setup, help)

    def extend_command(self, name: str, setup: Callable | None = None, before: Callable | None = None,
                       after: Callable | None = None) -> None:
        if name not in self._r.core_commands:
            raise ValueError(f"{name!r} is not a core command")
        self._r.extensions.append(_Extension(self.name, name, setup, before, after))

    def add_status_lines(self, fn: Callable) -> None:
        if not callable(fn):
            raise TypeError("fn must be callable")
        self._r.status_hooks.append((self.name, fn))

    def add_pipeline_recipe(self, fn: Callable) -> None:
        """fn(ctx, board, job, artifact) -> dict | None; first matching recipe wins."""
        if not callable(fn):
            raise TypeError("fn must be callable")
        self._r.pipeline_hooks.append((self.name, fn))

    def register_event_source(self, name: str, *, routes: list, verify: Callable, handle: Callable,
                              poll: Callable | None = None, poll_interval_s: int | None = None,
                              helpers: Callable | None = None) -> None:
        """An external event source for `swarm events serve`.

        routes: URL paths the listener hands to this source (e.g. ["/gitea"]).
        verify(headers, body) -> bool runs on the raw body BEFORE anything parses it.
        handle(headers, body, ctx) -> list[EventSpec]; EventSpec = dict(job, kind, key, text, to=None).
        poll(ctx) -> list[EventSpec] | None runs every poll_interval_s seconds (needs both).
        ctx (EventContext): .board, .config (this source's [events.sources.<name>]), .post, .log.
        helpers(config) -> list[dict(name, argv, env=None)]: per-source helper processes the listener
        keeps running (restart with backoff, health in the safety nets), e.g. `gh webhook forward`, an
        outbound websocket that delivers webhooks here. argv is a list (no shell); env adds to the
        listener's environment; never put a token in argv (use a token_file the helper reads).
        poll_interval_s has a floor ([events] min_poll_interval_s, 600) unless the source's config sets
        poll_interval_s itself.
        Core never interprets kind or the body."""
        if not isinstance(name, str) or not name.replace("-", "").replace("_", "").isalnum() or not name[:1].isalpha():
            raise ValueError(f"bad event source name {name!r}")
        if name in self._r.event_sources:
            raise ValueError(f"event source {name!r} already exists ({self._r.event_sources[name].plugin})")
        if (not isinstance(routes, (list, tuple)) or not routes
                or not all(isinstance(r, str) and r.startswith("/") and len(r) > 1 and "?" not in r for r in routes)):
            raise ValueError("routes must be a non-empty list of paths starting with /")
        taken = {r for src in self._r.event_sources.values() for r in src.routes}
        if taken & set(routes):
            raise ValueError(f"event route already taken: {sorted(taken & set(routes))[0]}")
        if not callable(verify) or not callable(handle):
            raise TypeError("verify and handle must be callable")
        if helpers is not None and not callable(helpers):
            raise TypeError("helpers must be callable")
        if poll is not None and not callable(poll):
            raise TypeError("poll must be callable")
        if poll is not None and (not isinstance(poll_interval_s, int) or isinstance(poll_interval_s, bool)
                                 or poll_interval_s < 1):
            raise ValueError("poll needs poll_interval_s, a positive integer")
        self._r.event_sources[name] = EventSource(self.name, name, tuple(routes), verify, handle, poll,
                                                  poll_interval_s, helpers)

    def register_blocker_kind(self, kind: str, display: Callable | None = None,
                              expiry: Callable | None = None, protection: str = "addressed") -> None:
        """display(ctx, blocker)->str; expiry(ctx, blocker) after a default expires it.
        Protection is persisted on each blocker: addressed protects person/role waits,
        always protects until resolution, deadline protects only before a future until.
        Core owns state and audit events. A plugin callback cannot veto or replace that state.
        """
        from .board.blockers import check_blocker
        check_blocker(kind, 'external', 'registration')
        if kind == 'wait' or kind in self._r.blocker_kinds:
            raise ValueError(f'blocker kind {kind!r} is already registered')
        if protection not in ("addressed", "always", "deadline"):
            raise ValueError(f'unknown blocker protection rule {protection!r}')
        for fn in (display, expiry):
            if fn is not None and not callable(fn): raise TypeError('callback must be callable')
        self._r.blocker_kinds[kind] = (self.name, display, expiry, protection)

    def add_blocker_event_hook(self, fn: Callable) -> None:
        """fn(ctx, BlockerEvent) after commit; notification adapters subscribe here."""
        self._add_hook(self._r.blocker_event_hooks, fn)

    def add_watch_pane(self, fn: Callable) -> None:
        """fn(ctx, job|None)->list[str], queried during snapshot capture, replayed for keys."""
        self._add_hook(self._r.watch_hooks, fn)

    def add_orchestrator_lines(self, fn: Callable) -> None:
        """fn(ctx, job)->list[str] appended to the orchestrator's tool-call context."""
        self._add_hook(self._r.orchestrator_hooks, fn)

    def add_agent_lines(self, fn: Callable) -> None:
        """fn(ctx, job, agent_key)->list[str], injected before the agent's next tool call."""
        self._add_hook(self._r.agent_hooks, fn)

    def _add_hook(self, hooks, fn):
        if not callable(fn): raise TypeError('fn must be callable')
        hooks.append((self.name, fn))


class Registry:
    def __init__(self, cfg: dict | None = None, config_path: Path | None = None,
                 core_commands: tuple[str, ...] = ()):
        self.cfg = cfg if cfg is not None else {}
        self.config_path = Path(config_path) if config_path else paths.config_path()
        self.core_commands = set(core_commands)
        self.plugins: list[PluginInfo] = []
        self.commands: dict[str, _Command] = {}
        self.extensions: list[_Extension] = []
        self.status_hooks: list[tuple[str, Callable]] = []
        self.pipeline_hooks: list[tuple[str, Callable]] = []

        self.blocker_kinds: dict[str, tuple] = {}
        self.blocker_event_hooks: list[tuple[str, Callable]] = []
        self.watch_hooks: list[tuple[str, Callable]] = []
        self.orchestrator_hooks: list[tuple[str, Callable]] = []
        self.agent_hooks: list[tuple[str, Callable]] = []
        self.event_sources: dict[str, EventSource] = {}

    # ---- loading

    def load(self) -> "Registry":
        disabled = set((self.cfg.get("plugins") or {}).get("disabled") or ())
        seen: set[str] = set()
        for name, source, loader in self._discover():
            if name in seen:
                self.plugins.append(PluginInfo(name, source, error="shadowed by an earlier plugin of the same name"))
                continue
            seen.add(name)
            if name in disabled:
                self.plugins.append(PluginInfo(name, source, error="disabled ([plugins] disabled)", disabled=True))
                continue
            self._load_one(name, source, loader)
        return self

    def _load_one(self, name: str, source: str, loader: Callable) -> None:
        info = PluginInfo(name, source)
        self.plugins.append(info)
        try:
            register = loader()
            register(PluginAPI(self, name, self.config_path.parent))
        except BaseException as exc:   # a broken plugin never breaks core (SystemExit included)
            if isinstance(exc, KeyboardInterrupt):
                raise
            info.error = f"{type(exc).__name__}: {exc}"[:300]
            # what it registered before failing is dropped: a half-loaded plugin is not loaded
            for cmd in [c for c in self.commands if self.commands[c].plugin == name]:
                del self.commands[cmd]
            self.extensions[:] = [e for e in self.extensions if e.plugin != name]
            self.status_hooks[:] = [h for h in self.status_hooks if h[0] != name]
            self.pipeline_hooks[:] = [h for h in self.pipeline_hooks if h[0] != name]

            self._drop_blocker_hooks(name)
            return
        info.commands = sorted(c for c, v in self.commands.items() if v.plugin == name)
        info.extends = sorted({e.command for e in self.extensions if e.plugin == name})
        info.sources = sorted(n for n, v in self.event_sources.items() if v.plugin == name)

    def _discover(self):
        dirs = [self.config_path.parent / "plugins"]
        dirs += [Path(p) for p in os.environ.get("SWARM_PLUGIN_PATH", "").split(os.pathsep) if p]
        for d in dirs:
            try:
                entries = sorted(d.iterdir()) if d.is_dir() else []
            except OSError:
                continue
            for p in entries:
                if p.suffix == ".py" and (p.is_file() or p.is_symlink()) and not p.name.startswith("_"):
                    yield p.stem, str(p), self._file_loader(p, p.stem)
                elif p.is_dir() and (p / "__init__.py").is_file() and not p.name.startswith(("_", ".")):
                    yield p.name, str(p), self._file_loader(p / "__init__.py", p.name)
        skills = paths.PLUGIN_ROOT / "skills"
        try:
            shipped = sorted(skills.glob(f"*/{SHIPPED_FILE}")) if skills.is_dir() else []
        except OSError:
            shipped = []
        for p in shipped:
            yield p.parent.name, str(p), self._file_loader(p, p.parent.name)
        yield from self._entry_points()

    def _entry_points(self):
        try:
            from importlib.metadata import entry_points
            eps = list(entry_points(group=ENTRY_POINT_GROUP))
        except Exception:
            return
        for ep in eps:
            yield ep.name, f"entry point {ep.name} = {ep.value}", self._ep_loader(ep)

    @staticmethod
    def _file_loader(path: Path, name: str) -> Callable:
        def load():
            if path.name == "__init__.py":
                _check_file_trust(path.parent)
            _check_file_trust(path)
            mod_name = f"swarm_plugin_{name.replace('-', '_')}"
            spec = importlib.util.spec_from_file_location(
                mod_name, path, submodule_search_locations=[str(path.parent)] if path.name == "__init__.py" else None)
            if spec is None or spec.loader is None:
                raise ImportError(f"cannot load {path}")
            mod = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = mod
            try:
                spec.loader.exec_module(mod)
            except BaseException:
                sys.modules.pop(mod_name, None)
                raise
            return _register_of(mod)
        return load

    @staticmethod
    def _ep_loader(ep) -> Callable:
        def load():
            obj = ep.load()
            return obj if callable(obj) and not hasattr(obj, "register") else _register_of(obj)
        return load

    # ---- the CLI's side

    def apply(self, subparsers) -> None:
        """Add the plugins' commands and extra arguments to the parser (after the core's own)."""
        for cmd in list(self.commands.values()):
            try:
                p = subparsers.add_parser(cmd.name, help=cmd.help or f"(plugin {cmd.plugin})")
                if cmd.setup:
                    cmd.setup(p)
            except BaseException as exc:
                if isinstance(exc, KeyboardInterrupt):
                    raise
                self._fail(cmd.plugin, f"setting up command {cmd.name}: {exc}")
                subparsers.choices.pop(cmd.name, None)
                self.commands.pop(cmd.name, None)
        for ext in list(self.extensions):
            if ext.setup is None:
                continue
            try:
                ext.setup(subparsers.choices[ext.command])
            except BaseException as exc:
                if isinstance(exc, KeyboardInterrupt):
                    raise
                self._fail(ext.plugin, f"extending {ext.command}: {exc}")
                self.extensions.remove(ext)

    def _fail(self, plugin: str, why: str) -> None:
        for info in self.plugins:
            if info.name == plugin and info.error is None:
                info.error = why[:300]
        self.status_hooks[:] = [h for h in self.status_hooks if h[0] != plugin]
        self.pipeline_hooks[:] = [h for h in self.pipeline_hooks if h[0] != plugin]

        self._drop_blocker_hooks(plugin)
        self.commands = {k: v for k, v in self.commands.items() if v.plugin != plugin}
        self.extensions[:] = [e for e in self.extensions if e.plugin != plugin]

    def context(self, plugin: str, board=None) -> PluginContext:
        return PluginContext(self.cfg, self.config_path, plugin, board, self)

    def run_command(self, name: str, args) -> int:
        cmd = self.commands[name]
        try:
            return int(cmd.run(self.context(cmd.plugin), args) or 0)
        except SystemExit:
            raise
        except Exception as exc:
            print(f"swarm {name}: plugin {cmd.plugin} failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1

    def run_before(self, command: str, args) -> int:
        """0 to go on; a plugin's non-zero status stops the command. A hook that raises is a warning."""
        for ext in self.extensions:
            if ext.command == command and ext.before:
                try:
                    rc = ext.before(self.context(ext.plugin), args)
                except Exception as exc:
                    self._warn(ext.plugin, command, exc)
                    continue
                if rc:
                    return int(rc)
        return 0

    def run_after(self, command: str, args) -> None:
        for ext in self.extensions:
            if ext.command == command and ext.after:
                try:
                    ext.after(self.context(ext.plugin), args)
                except Exception as exc:
                    self._warn(ext.plugin, command, exc)

    def status_lines(self, job: str, board=None) -> list[str]:
        out: list[str] = []
        for name, fn in self.status_hooks:
            try:
                out += [str(line) for line in fn(self.context(name, board), job) or ()]
            except Exception as exc:
                self._warn(name, "status", exc)
        return out

    def pipeline_recipe(self, board, job: str, artifact: str | None) -> dict:
        for name, fn in self.pipeline_hooks:
            recipe = fn(self.context(name, board), board, job, artifact)
            if recipe is not None:
                if not isinstance(recipe, dict):
                    raise ValueError(f"plugin {name}: pipeline recipe must return a dict or None")
                return recipe
        return {}

    def _drop_blocker_hooks(self, plugin):
        for hooks in (self.blocker_event_hooks, self.watch_hooks, self.orchestrator_hooks, self.agent_hooks):
            hooks[:] = [h for h in hooks if h[0] != plugin]
        self.blocker_kinds = {k:v for k,v in self.blocker_kinds.items() if v[0] != plugin}
        self.event_sources = {k: v for k, v in self.event_sources.items() if v.plugin != plugin}

    def _lines(self, hooks, what, job, board):
        out = []
        for name, fn in hooks:
            try:
                out.extend(str(line) for line in fn(self.context(name, board), job) or ())
            except Exception as exc:
                self._warn(name, what, exc)
        return out

    def watch_panes(self, job=None, board=None):
        return self._lines(self.watch_hooks, 'watch', job, board)

    def orchestrator_lines(self, job, board=None):
        return self._lines(self.orchestrator_hooks, 'orchestrator', job, board)

    def agent_lines(self, job, agent_key, board=None):
        out = []
        for name, fn in self.agent_hooks:
            try:
                out.extend(str(line) for line in fn(self.context(name, board), job, agent_key) or ())
            except Exception as exc:
                self._warn(name, 'agent context', exc)
        return out

    def blocker_event(self, board, event):
        for name, fn in self.blocker_event_hooks:
            try: fn(self.context(name, board), event)
            except Exception as exc: self._warn(name, 'blocker event', exc)

    def blocker_protection(self, kind):
        registered = self.blocker_kinds.get(kind)
        return registered[3] if registered else "addressed"

    def blocker_display(self, board, blocker):
        registered = self.blocker_kinds.get(blocker.kind)
        if registered and registered[1]:
            name, fn, _, _ = registered
            try: return str(fn(self.context(name, board), blocker))
            except Exception as exc: self._warn(name, 'blocker display', exc)
        return f'{blocker.id} {blocker.kind} -> {blocker.waiting_on}: {blocker.reason}'

    def blocker_expired(self, board, blocker):
        registered = self.blocker_kinds.get(blocker.kind)
        if registered and registered[2]:
            name, _, fn, _ = registered
            try: fn(self.context(name, board), blocker)
            except Exception as exc: self._warn(name, 'blocker expiry', exc)

    @staticmethod
    def _warn(plugin: str, what: str, exc: Exception) -> None:
        print(f"swarm: plugin {plugin}: {what} hook failed: {type(exc).__name__}: {exc}", file=sys.stderr)

    def report(self) -> list[str]:
        """The lines `swarm plugins` prints."""
        if not self.plugins:
            return ["no plugins found (looked in " + ", ".join(str(d) for d in self.search_dirs()) + ")"]
        lines = []
        for p in self.plugins:
            what = ", ".join([*p.commands, *(f"+{c}" for c in p.extends), *(f"events:{n}" for n in p.sources)]) or "-"
            state = "loaded" if p.loaded else "disabled" if p.disabled else "ERROR"
            lines.append(f"{p.name}\t{state}\t{what}\t{p.source}")
            if p.error and not p.disabled:
                lines.append(f"  {p.name}: {p.error}")
        return lines

    def search_dirs(self) -> list[Path]:
        extra = [Path(p) for p in os.environ.get("SWARM_PLUGIN_PATH", "").split(os.pathsep) if p]
        return [self.config_path.parent / "plugins", *extra, paths.PLUGIN_ROOT / "skills" / "*"]


def _register_of(mod) -> Callable:
    fn = getattr(mod, "register", None)
    if not callable(fn):
        raise AttributeError("no register(api) function")
    return fn


def pipeline_recipe(cfg: dict, board, job: str, artifact: str | None, config_path=None) -> dict:
    """Load trusted plugins for a pipeline transition; recipe errors hold the transition.

    No command runs here. Evidence commands and finalization instructions go in the
    agent's brief, under its ordinary permissions.
    """
    from swarm.cli import _parser
    commands = tuple(_parser()._swarm_subparsers.choices)
    return Registry(cfg, config_path=config_path, core_commands=commands).load().pipeline_recipe(board, job, artifact)
