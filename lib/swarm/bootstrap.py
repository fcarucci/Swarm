"""First-run and post-update setup ("Bootstrap"), migrate (retiring the old skill install), doctor.

bootstrap() is idempotent. The launcher `bin/swarm` has already made sure the venv exists and
matches requirements.txt before any of this runs (it is running in it). Output never contains
secrets: board errors are reported by type only."""
from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from swarm import channel, compat, paths, safefs
from swarm.board import board_backend

# The last line must stay `exec "<root>/bin/swarm" "$@"`: launcher_target parses it. Everything
# before it is the self-healing fallback (POSIX sh: macOS bash 3.2, dash), used only when that
# target is missing or not executable. Braces are doubled because callers use LAUNCHER.format(root=).
LAUNCHER = (
    '#!/bin/sh\n'
    '# swarm launcher, written by `swarm bootstrap`: runs the installed swarm plugin, or the newest\n'
    '# installed one if that plugin folder is gone (a host replaced it).\n'
    'swarm_newest() {{\n'
    '  best=\n'
    '  for d in "${{CLAUDE_CONFIG_DIR:-$HOME/.claude}}"/plugins/cache/*/swarm/* '
    '"${{CODEX_HOME:-$HOME/.codex}}"/plugins/cache/*/swarm/*; do\n'
    '    [ -x "$d/bin/swarm" ] || continue\n'
    '    k=; o=$IFS; IFS=.; set -- ${{d##*/}}; IFS=$o\n'
    '    for p in "$@"; do\n'
    '      case $p in ""|*[!0-9]*) p=0;; esac\n'
    '      p=00000000$p; k=$k${{p#"${{p%????????}}"}}.\n'
    '    done\n'
    '    best="$best$k $d/bin/swarm\n"\n'
    '  done\n'
    '  best=$(printf "%s" "$best" | LC_ALL=C sort | tail -n 1)\n'
    '  best=${{best#* }}\n'
    '}}\n'
    'if [ ! -x "{root}/bin/swarm" ]; then\n'
    '  swarm_newest\n'
    '  if [ -z "$best" ]; then\n'
    '    echo "swarm: the installed plugin is gone; rerun install.sh to reinstall it." >&2\n'
    '    exit 127\n'
    '  fi\n'
    '  exec "$best" "$@"\n'
    'fi\n'
    'exec "{root}/bin/swarm" "$@"\n')
# Windows: ~/.local/bin/swarm.cmd. The `"<root>\\bin\\swarm.cmd" %*` line is the one launcher_target
# parses; the lines after :heal are the self-healing fallback (the newest installed plugin, by
# version, via PowerShell), used only when that target is gone.
LAUNCHER_CMD = (
    '@echo off\r\n'
    'rem swarm launcher, written by `swarm bootstrap`: runs the installed swarm plugin, or the newest\r\n'
    'rem installed one if that plugin folder is gone (a host replaced it).\r\n'
    'if not exist "{root}\\bin\\swarm.cmd" goto heal\r\n'
    '"{root}\\bin\\swarm.cmd" %*\r\n'
    'exit /b %ERRORLEVEL%\r\n'
    ':heal\r\n'
    'set "SWARM_BEST="\r\n'
    'for /f "usebackq delims=" %%B in (`powershell -NoProfile -Command "$c=$env:CLAUDE_CONFIG_DIR; '
    'if(-not $c){{$c=Join-Path $HOME \'.claude\'}}; $x=$env:CODEX_HOME; if(-not $x){{$x=Join-Path $HOME \'.codex\'}}; '
    'Get-ChildItem -Path (Join-Path $c \'plugins\\cache\\*\\swarm\\*\'),(Join-Path $x \'plugins\\cache\\*\\swarm\\*\') '
    '-Directory -ErrorAction SilentlyContinue | Where-Object {{Test-Path (Join-Path $_.FullName \'bin\\swarm.cmd\')}} '
    '| Sort-Object {{try{{[version]$_.Name}}catch{{[version]\'0.0\'}}}} | Select-Object -Last 1 -ExpandProperty FullName"`) '
    'do set "SWARM_BEST=%%B"\r\n'
    'if defined SWARM_BEST goto run\r\n'
    'echo swarm: the installed plugin is gone; rerun install.ps1 to reinstall it. 1>&2\r\n'
    'exit /b 127\r\n'
    ':run\r\n'
    '"%SWARM_BEST%\\bin\\swarm.cmd" %*\r\n'
    'exit /b %ERRORLEVEL%\r\n')
FILL_IN = ("[board] backend (\"file\" is the default: nothing to fill in), or for a shared board "
           "backend = \"postgres\" with [database] host, user, dbname and password_env_file "
           "(a chmod-600 file with PGPASSWORD=...)")


@dataclass(frozen=True)
class Step:
    name: str
    status: str      # ok | changed | skipped | failed | refused | manual
    detail: str = ""


_STEP_COLORS = {"ok": 32, "changed": 36, "manual": 33, "skipped": 33, "refused": 33, "failed": 31}


def format_steps(steps: list[Step], color: bool = False) -> str:
    """Plain text is byte-identical whether or not `color` is passed. `color=True` colours the
    status word (ok green, changed cyan, manual/skipped/refused yellow, failed red) and bolds the
    step name; reuses cli._sgr/_bold. Shared by `bootstrap`, `migrate` and the venv/launcher/host
    lines `doctor` prints alongside its checks."""
    from swarm.cli import _bold, _sgr
    lines = []
    for s in steps:
        name = _bold(f"{s.name:<8}", color)
        status = f"{s.status:<8}"
        if color and s.status in _STEP_COLORS:
            status = _sgr(_STEP_COLORS[s.status], status)
        lines.append(f"{name} {status} {s.detail}".rstrip())
    return "\n".join(lines)


def _version_tuple(v: str) -> tuple:
    parts = []
    for p in v.split("."):
        parts.append(int(p) if p.isdigit() else 0)
    return tuple(parts)


def launcher_target(path: Path) -> Path | None:
    """The plugin root a launcher written by ensure_launcher runs: the `exec "<root>/bin/swarm" "$@"`
    line of the sh launcher, or the `"<root>\\bin\\swarm.cmd" %*` line of the Windows one."""
    try:
        for line in path.read_text(errors="replace").splitlines():
            if line.startswith('exec "') and line.endswith('/bin/swarm" "$@"'):
                return Path(line[len('exec "'):-len('/bin/swarm" "$@"')])
            if line.startswith('"') and line.endswith('\\bin\\swarm.cmd" %*') and not line.startswith('"%'):
                return Path(line[1:-len('\\bin\\swarm.cmd" %*')])
    except OSError:
        pass
    return None


def _same_tree(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return a == b


def _claude_config_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude").expanduser()


def _codex_installed_version() -> str | None:
    """The version `codex plugin list --json` reports as installed for swarm, or None (no codex
    on PATH, the command failed, the plugin isn't installed, or an answer we can't parse). Reuses
    the same entry shape plugin_json_state reads (pluginId/name/installed/enabled), plus each
    installed entry's own "version" field."""
    import subprocess
    try:
        res = subprocess.run(["codex", "plugin", "list", "--json"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    try:
        data = json.loads(res.stdout)
    except ValueError:
        return None
    for e in (data.get("installed") or []) if isinstance(data, dict) else []:
        if isinstance(e, dict) and _is_swarm(e) and e.get("installed") is True:
            v = e.get("version")
            if v:
                return str(v)
    return None


def _registered_plugin_roots() -> set[Path]:
    """Plugin roots a host's own plugin manager currently reports as installed for swarm: every
    installPath in Claude's installed_plugins.json for a "swarm@<marketplace>" key (install.sh
    already parses this file the same way), plus whichever of Codex's plugin cache directories
    under <CODEX_HOME>/plugins/cache/<marketplace>/swarm/ matches the version `codex plugin list
    --json` reports as installed (Codex has no installed_plugins.json equivalent, so this is the
    closest thing to it -- every *other* cache directory is just a leftover, not something Codex
    still considers installed). Only a root in this set can ever count as "the newer, currently
    installed version" in ensure_launcher: a tree that merely claims a higher version number in
    its own plugin.json (a stale cache leftover from a previous install, or an old frozen
    checkout) must not keep the launcher pinned to it forever."""
    out: set[Path] = set()
    try:
        data = json.loads((_claude_config_dir() / "plugins" / "installed_plugins.json").read_text())
        for key, installs in (data.get("plugins") or {}).items():
            if key.split("@")[0] != "swarm":
                continue
            for inst in installs or []:
                if isinstance(inst, dict) and inst.get("installPath"):
                    out.add(Path(inst["installPath"]))
    except (OSError, ValueError, AttributeError):
        pass
    try:
        installed_v = _codex_installed_version()
        if installed_v:
            for p in _codex_home().glob("plugins/cache/*/swarm/*"):
                if p.is_dir() and paths.plugin_version(p) == installed_v:
                    out.add(p)
    except OSError:
        pass
    return out


_PYC_NAME = re.compile(r"^(?P<stem>[^.]+)\.[A-Za-z0-9_-]+-\d+(?:\.opt-\d+)?\.pyc$")   # x.cpython-313[.opt-1].pyc


def _prunable_cache(root: Path) -> Path | None:
    """The cache directory to prune, resolved, or None when it may not be pruned: not absolute,
    not private (paths.private_dir), or not strictly inside the home directory (or inside the
    default cache, ~/.local/share/swarm/pyc). The home directory and every ancestor of it are
    refused by identity (st_dev, st_ino), so no spelling reaches them (`//home/u`,
    `/proc/self/root/home/u`, a symlinked parent). Any error refuses."""
    try:
        root = Path(root)
        if not root.is_absolute() or not paths.private_dir(root):
            return None
        real = Path(os.path.realpath(root))
        home = Path(os.path.realpath(os.path.expanduser("~")))
        default = Path(os.path.realpath(paths.share_dir() / "pyc"))
        if not (home in real.parents or real == default or default in real.parents):
            return None
        st = os.stat(real)
        for anc in (home, *home.parents):
            a = os.stat(anc)
            if (a.st_dev, a.st_ino) == (st.st_dev, st.st_ino):
                return None
        if not paths.private_dir(real):
            return None
        return real
    except (OSError, ValueError, RuntimeError):
        return None


def prune_pycache(root: Path | None = None) -> int:
    """Delete cached bytecode whose source no longer exists (an old plugin version, a removed
    module) from the launchers' cache; returns how many files went. Best effort.

    The cache ($SWARM_PYCACHE, else ~/.local/share/swarm/pyc) is pruned only when it passes the
    launchers' own check (paths.private_dir: this user's, 0700-style, no symlink, no ACL) and lies
    strictly inside the home directory, never the home directory or above it (_prunable_cache).
    Only interpreter cache files (<module>.<tag>-NN[.opt-N].pyc) are deleted, and only a directory that this prune emptied is removed: a directory that was
    empty before, or holds anything else, stays. Never follows symlinks, never leaves the cache."""
    root = _prunable_cache(root or paths.pycache_dir())
    if root is None:
        return 0
    gone = 0
    emptied: set[Path] = set()
    for d, dirs, files in os.walk(root, topdown=False, followlinks=False):
        here = Path(d)
        src = Path("/") / here.relative_to(root)
        for name in files:
            m = _PYC_NAME.match(name)
            if not m or (here / name).is_symlink():
                continue
            if not (src / (m.group("stem") + ".py")).exists():
                try:
                    (here / name).unlink()
                    gone += 1
                    emptied.add(here)
                except OSError:
                    pass
        if here == root or not (here in emptied or any(here / x in emptied for x in dirs)):
            continue
        try:
            if not here.is_symlink() and not any(here.iterdir()):
                here.rmdir()
                emptied.add(here)
        except OSError:
            pass
    return gone


def ensure_launcher(root: Path = paths.PLUGIN_ROOT) -> Step:
    """~/.local/bin/swarm runs this plugin: written when missing, when its target is gone, or
    when its target is another tree that isn't a newer, currently-installed plugin root. A
    same-version target elsewhere (an old frozen tree or checkout also calling itself 0.1.0) is
    repointed too: comparing versions alone would leave the launcher, and the supervisor and CLI
    behind it, running that other tree forever. A higher version number alone isn't enough either
    -- a stale cache directory from a previous install (e.g. an old .../swarm/1.0.0 left behind
    after upgrading to 0.1.0) would otherwise outrank the fresh install and never let go. Only a
    target that is both strictly newer *and* a root this host's plugin manager currently reports
    as installed (see _registered_plugin_roots) is kept."""
    lp = paths.launcher_path()
    target = launcher_target(lp) if lp.exists() else None
    if target is not None and _same_tree(target, root):
        return Step("launcher", "ok", str(lp))
    if target is not None and (target / "bin" / "swarm").exists() \
            and _version_tuple(paths.plugin_version(target)) > _version_tuple(paths.plugin_version(root)) \
            and any(_same_tree(target, r) for r in _registered_plugin_roots()):
        return Step("launcher", "ok", f"{lp} -> {target} (newer than this plugin)")
    if lp.exists() and target is None:
        return Step("launcher", "manual", f"{lp} exists and is not a swarm launcher: left alone")
    lp.parent.mkdir(parents=True, exist_ok=True)
    tmp = lp.with_name(lp.name + ".swarm-tmp")
    if paths.IS_WINDOWS:
        tmp.write_text(LAUNCHER_CMD.format(root=root), newline="")   # CRLF kept: cmd's goto labels need it
    else:
        tmp.write_text(LAUNCHER.format(root=root))
    tmp.chmod(0o755)
    os.replace(tmp, lp)
    return Step("launcher", "changed", f"{lp} -> {root}")


def ensure_config(path: Path) -> Step:
    example = paths.PLUGIN_ROOT / "config.example.toml"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(example, path)
        path.chmod(0o600)
        # the example says [board] backend = "file" explicitly: a new user gets the file board,
        # which needs no server and no credentials, so there is nothing to fill in
        return Step("config", "changed", f"created {path} from the example (file board; set backend = \"postgres\" "
                                         "and [database] for a shared one)")
    return Step("config", "ok", str(path))


def ensure_board(cfg: dict) -> Step:
    from swarm.board import BoardUnavailable, SCHEMA_VERSION, ensure_initialized
    try:
        try:
            res = ensure_initialized(cfg, timeout=30.0)
        except BoardUnavailable:
            from swarm.board import autoinit      # a store dropped behind its stamp: set up again, like open_board
            if not autoinit.recover_missing(cfg, 30.0):
                raise
            res = ensure_initialized(cfg, timeout=30.0)
    except BoardUnavailable as exc:
        return Step("board", "failed", f"unreachable ({type(exc.__cause__ or exc).__name__}); `swarm doctor` for details")
    except Exception as exc:
        return Step("board", "failed", f"{type(exc).__name__}")
    if res.action == "disabled":        # SWARM_AUTO_INIT=0: nothing was touched, so no stamp either
        return Step("board", "refused", "automatic setup is off (SWARM_AUTO_INIT=0); `swarm init` sets the board up")
    if res.action == "newer":
        return Step("board", "manual", f"schema {res.version} is newer than this plugin's {SCHEMA_VERSION}: update the plugin")
    return Step("board", "changed" if res.action == "initialized" else "ok", f"schema {SCHEMA_VERSION} ({res.action})")


OLD_SPOOL = "/tmp/claude/swarm-spool"      # the shared default before 0.1.0: never granted


def spool_problem(cfg: dict) -> str | None:
    """Why the configured spool dir isn't private to this OS user, or None. The spool is a
    queue the unsandboxed hooks act on: whoever controls a directory on its path controls what
    the leaf resolves to. A path outside $HOME must name this uid, so another user can't create
    it first; no component may be a symlink or belong to another user (root's are fine)."""
    from swarm import codex_config
    spool = codex_config.spool_path(cfg)
    if str(spool) == OLD_SPOOL:
        return f"{spool} is the old shared default, not per-user"
    home = paths.home()
    uid = compat.uid()
    import re
    # (Windows has no uid: a spool outside the profile is judged by its links alone.)
    if not paths.IS_WINDOWS and spool != home and home not in spool.parents \
            and not re.search(rf"(?<![0-9]){uid}(?![0-9])", str(spool)):
        return f"{spool} is outside your home and doesn't name your uid ({uid}), so it isn't per-user"
    cur = Path(spool.anchor or "/")
    for part in spool.parts[1:]:
        cur = cur / part
        try:
            st = os.lstat(cur)
        except FileNotFoundError:
            break
        except OSError as exc:
            return f"can't check {cur} ({exc.strerror or type(exc).__name__})"
        import stat as _stat
        # a root-owned symlink (macOS /tmp -> /private/tmp) can't be swapped by another user; on
        # Windows a symlink or junction (reparse point) anywhere below the profile is refused
        link = _stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & 0x400)
        if link and (st.st_uid != 0 or paths.IS_WINDOWS) and cur not in (home, *home.parents):
            return f"{cur} is a symlink"
        if st.st_uid not in (0, uid):
            return f"{cur} belongs to another user (uid {st.st_uid})"
    return None


def claude_sandbox_step(cfg: dict, settings_path: Path | None = None) -> Step:
    """Claude Code's sandbox writes to the per-user spool dir: add it to
    sandbox.filesystem.allowWrite in the Claude user settings (the key of Claude Code 2.1.283),
    through safefile (backup first, mode kept), keeping every other key. The dir is created 0700
    first. A settings file the swarm can't parse, or whose sandbox keys have another shape, is
    left alone: the step says what to add by hand."""
    from swarm import cli, codex_config, safefile
    settings_path = settings_path or cli.claude_settings_path()
    spool = str(codex_config.spool_path(cfg))
    by_hand = f'add "{spool}" to sandbox.filesystem.allowWrite in {settings_path}'
    problem = spool_problem(cfg)
    if problem:
        return Step("host", "manual", f"claude: [board] spool_dir: {problem}; set it to a per-user dir "
                                      f"(default ~/.local/state/swarm/spool), then run `swarm bootstrap`")
    settings = {}
    try:
        if settings_path.exists():
            text = settings_path.read_text()
            settings = json.loads(text) if text.strip() else {}
    except (OSError, ValueError) as exc:
        return Step("host", "manual", f"claude: can't read {settings_path} ({_error_kind(exc)}); {by_hand}")
    sandbox = settings.get("sandbox", {}) if isinstance(settings, dict) else None
    fs = sandbox.get("filesystem", {}) if isinstance(sandbox, dict) else None
    allow = fs.get("allowWrite", []) if isinstance(fs, dict) else None
    if not isinstance(allow, list) or not all(isinstance(a, str) for a in allow):
        return Step("host", "manual", f"claude: {settings_path} has a sandbox setting of another shape; {by_hand}")
    problem = codex_config._ensure_private_root(Path(spool))
    if problem:
        return Step("host", "manual", f"claude: can't use the spool dir {spool} ({problem})")
    if spool in allow:
        return Step("host", "ok", f"claude: the sandbox may write the spool dir {spool}")
    new = {**settings, "sandbox": {**sandbox, "filesystem": {**fs, "allowWrite": allow + [spool]}}}
    saved = safefile.backup(settings_path)
    safefile.write_preserving(settings_path, json.dumps(new, indent=2) + "\n")
    return Step("host", "ok", f"claude: added {spool} to sandbox.filesystem.allowWrite in {settings_path}"
                              + (f" (backup: {saved.name})" if saved else ""))


def host_setup(host: str | None, cfg: dict | None = None) -> Step:
    """cfg: the swarm config bootstrap() loaded (its --config), for the Codex writable roots and
    the Claude sandbox's spool entry."""
    if host == "claude":
        from swarm.cli import load_config
        return claude_sandbox_step(cfg or load_config(paths.config_path()))
    if host == "codex":
        from swarm import codex_config
        from swarm.cli import load_config
        status, detail = codex_config.apply(_codex_home() / "config.toml", cfg or load_config(paths.config_path()))
        if status == "changed":   # Codex reads config.toml at session start: running sessions keep the old sandbox
            return Step("host", "manual", f"{detail}\n{NEW_SESSION_STEP}\n{TRUST_STEP}")
        if status == "manual" or not _hooks_ran("codex"):
            return Step("host", "manual", f"{detail}\n{TRUST_STEP}")
        return Step("host", "ok", detail)
    return Step("host", "skipped", "no host given")


TRUST_STEP = ("Codex: open /hooks in a Codex session and trust the swarm plugin's hooks (Codex asks "
              "again after every plugin update that changes hooks/codex-hooks.json).")
NEW_SESSION_STEP = ("Codex: these sandbox settings apply to Codex sessions started from now on; start a "
                    "new Codex session before running a swarm (the current one keeps its old sandbox).")


def _codex_home() -> Path:
    from swarm.hosts.codex import codex_home      # one definition
    return codex_home()


def _hooks_ran(host: str) -> bool:
    """Whether bin/swarm-hook's session-start has stamped this version for `host`: the stamps are
    in the private host dir (a sandboxed agent can't fake one there)."""
    prefix = f"hooks-ran-{host}-{paths.plugin_version()}-"
    try:
        d = safefs.open_base(paths.host_dir(), create=False)
    except (OSError, ValueError):
        return False
    try:
        return any(n.startswith(prefix) and safefs.exists(d, n) for n in compat.listdir(d))
    except OSError:
        return False
    finally:
        os.close(d)


def _host_fd(create: bool = True) -> int:
    """The private host dir (~/.local/share/swarm/host, 0700), opened without following links."""
    return safefs.open_base(paths.host_dir(), create=create, strict_mode=0o700)


LEGACY_HOOK_ARGS = ("start", "turn", "done", "stop")   # what the old install_hooks registered


def legacy_hook_set(skills_dir: Path | None = None) -> set[str]:
    """The exact commands the old skill's install_hooks wrote: "<skill dir>/bin/swarm-hook <arg>",
    with the skill dir as ~/.claude/skills/swarm and as its resolved path. Nothing else counts."""
    base = (skills_dir or paths.home() / ".claude" / "skills") / "swarm"
    dirs = {str(base)}
    try:
        dirs.add(str(base.resolve()))
    except OSError:
        pass
    dirs |= {Path(d).as_posix() for d in dirs}   # Windows: the same path with forward slashes
    return {f"{d}/bin/swarm-hook {a}" for d in dirs for a in LEGACY_HOOK_ARGS}


def legacy_hook_commands(settings: dict, skills_dir: Path | None = None) -> list[str]:
    legacy = legacy_hook_set(skills_dir)
    return [h.get("command", "") for groups in (settings.get("hooks") or {}).values()
            for g in groups for h in g.get("hooks", []) if (h.get("command") or "").strip() in legacy]


def _without_swarm_hooks(settings: dict, skills_dir: Path | None = None) -> dict:
    """The settings minus the old skill's hook commands. Only what was ours goes: a group, an
    event or the "hooks" key is dropped only when removing our commands left it empty; the
    user's own empty groups and events stay as they were."""
    legacy = legacy_hook_set(skills_dir)
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return settings
    new_hooks = {}
    for event, groups in hooks.items():
        kept, removed = [], False
        for g in groups:
            hs = g.get("hooks", [])
            rest = [h for h in hs if (h.get("command") or "").strip() not in legacy]
            if len(rest) == len(hs):
                kept.append(g)
            else:
                removed = True
                if rest:
                    kept.append({**g, "hooks": rest})
        if kept or not removed:
            new_hooks[event] = kept
    if new_hooks or not hooks:
        return {**settings, "hooks": new_hooks}
    return {k: v for k, v in settings.items() if k != "hooks"}


class UnsafeMarkerDir(Exception):
    """The marker dir can't be read safely (a symlink, another user's, a loose directory on its
    path): whether a job is active there is unknown. Not the same thing as an active job."""


def active_jobs(marker_dir: Path) -> list[str]:
    """The jobs of the markers in `marker_dir` (sandbox-writable): read through safefs, so a FIFO
    never blocks and a link is never followed. A marker that can't be read (a FIFO, link,
    garbage) still counts, by its file name. UnsafeMarkerDir when the directory itself can't be
    used (never reported as an active job)."""
    import json
    from swarm import safefs
    try:
        d = safefs.open_base(marker_dir, create=False)
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        raise UnsafeMarkerDir(str(exc)) from exc
    jobs, skipped = [], []
    try:
        found = safefs.scan(d, ".json", limit=64 * 1024, skipped=skipped)
    finally:
        os.close(d)
    for name, data in found:
        try:
            m = json.loads(data)
            jobs.append((m.get("job") if isinstance(m, dict) else None) or name[:-5])
        except ValueError:
            jobs.append(name[:-5])
    return jobs + [n[:-5] for n in skipped]


def marker_dir_of(cfg: dict) -> Path:
    """Where the given swarm config's active-job markers live."""
    return Path(cfg["hook"]["marker_dir"]).expanduser()


OLD_BOARDS = {"sqlite": "~/.local/state/swarm/board.sqlite3",     # the defaults before 0.1.0: inside
              "file": "~/.local/state/swarm/board"}                 # the old Codex writable root


def _old_board(cfg: dict) -> tuple[Path, Path] | None:
    """(old, new) when the configured local board isn't at the old default but one is there."""
    backend = board_backend(cfg)
    if backend not in OLD_BOARDS:
        return None
    old = Path(os.path.expanduser(OLD_BOARDS[backend]))
    new = Path(os.path.abspath(os.path.expanduser(cfg[backend]["path"])))
    if new == old or not os.path.lexists(old):
        return None
    return old, new


def _old_spool_pending(cfg: dict) -> bool:
    """Whether the old shared spool is there to empty: a real directory (not a link) of this
    user's, and not the configured spool. Another user's is theirs."""
    import stat as _stat
    from swarm import codex_config
    if str(codex_config.spool_path(cfg)) == OLD_SPOOL:
        return False
    try:
        st = os.lstat(OLD_SPOOL)
    except OSError:
        return False
    return _stat.S_ISDIR(st.st_mode) and st.st_uid == compat.uid()


LOCAL_DIRS = (".local", ".local/share", ".local/state", ".local/state/swarm", ".local/share/swarm")


def _local_dirs(cfg: dict | None) -> list[str]:
    """LOCAL_DIRS plus the marker and spool dirs (the config's, else the defaults) and every
    directory between ~/.local and them, parents first, relative to $HOME. A marker or spool dir
    outside ~/.local is left alone (a directory the user chose, maybe shared on purpose; doctor
    reports it)."""
    from swarm import cli
    cfg = cfg or {}
    out = list(LOCAL_DIRS)
    home = os.path.normpath(str(paths.home()))
    for section, key in (("hook", "marker_dir"), ("board", "spool_dir")):
        raw = (cfg.get(section) or {}).get(key) or cli.DEFAULTS[section][key]
        p = os.path.normpath(os.path.expanduser(str(raw).replace("{uid}", str(compat.uid()))))
        rel = os.path.relpath(p, home) if os.path.isabs(p) else ""
        parts = rel.split("/")
        if len(parts) < 2 or parts[0] != ".local" or ".." in parts:
            continue
        for i in range(2, len(parts) + 1):
            sub = "/".join(parts[:i])
            if sub not in out:
                out.append(sub)
    return out


def tighten_local_dirs(cfg: dict | None = None) -> Step | None:
    """safefs refuses a path through a group- or world-writable directory of this
    user, and a umask of 002 leaves ~/.local, ~/.local/share and ~/.local/state 0775 (and the
    swarm's own dirs below them), which makes the hooks inert and the board, spool and markers
    unusable. Take go-w off each of _local_dirs(cfg) that is a real directory (every component
    reached from $HOME with O_NOFOLLOW, never through a symlink) owned by this user; nothing else
    is touched, nothing is backed up (a mode change). A Step saying what changed (one line), or
    None when there was nothing to do. None on Windows: there are no group/world write bits;
    the profile's NTFS ACL is what keeps other users out."""
    if paths.IS_WINDOWS:
        return None
    import stat as _stat
    fixed, failed = [], []
    flags = os.O_RDONLY | compat.O_DIRECTORY | compat.O_NOFOLLOW | compat.O_CLOEXEC
    try:
        home_fd = compat.open_root(paths.home())
    except OSError:
        return None
    opened = {"": home_fd}
    try:
        for rel in _local_dirs(cfg):
            parent, _, name = rel.rpartition("/")
            if parent not in opened:
                continue   # its parent isn't a usable directory (missing, a symlink, another user's)
            try:
                fd = compat.open(name, flags, dir_fd=opened[parent])
            except OSError:
                continue
            opened[rel] = fd
            st = os.fstat(fd)
            if st.st_uid != compat.uid():
                os.close(opened.pop(rel))
                continue
            if st.st_mode & 0o022 and not st.st_mode & _stat.S_ISVTX:
                try:
                    compat.fchmod(fd, _stat.S_IMODE(st.st_mode) & ~0o022)
                    fixed.append(f"~/{rel}")
                except OSError as exc:
                    failed.append(f"~/{rel} ({exc.strerror or type(exc).__name__})")
    finally:
        for fd in opened.values():
            os.close(fd)
    if failed:
        return Step("local dirs", "failed", f"can't chmod go-w {', '.join(failed)}: the swarm refuses "
                    f"paths through group- or world-writable directories; fix it by hand")
    if fixed:
        return Step("local dirs", "changed", f"chmod go-w {' '.join(fixed)} (they were group- or "
                    f"world-writable, which the swarm refuses)")
    return None


def migrate(**kw) -> list[Step]:
    """_migrate, after tighten_local_dirs (its line first, when it changed something). On Windows
    there is nothing old to retire (no old skill install, old default board or shared spool), so
    it finds nothing to do."""
    tight = tighten_local_dirs(kw.get("cfg"))
    return ([tight] if tight else []) + _migrate(**kw)


def _migrate(*, force: bool = False, settings_path: Path | None = None, skills_dir: Path | None = None,
             marker_dir: Path | None = None, dest: Path | None = None, cfg: dict | None = None,
             codex_config_path: Path | None = None) -> list[Step]:
    """Retire the old ~/.claude/skills/swarm install: its exact hook commands out of the Claude
    settings (backup first, via safefile: the only writer of that file), its directory moved to
    ~/.local/share/swarm/legacy-skill-<time>. Then what 0.1.0 moved out of the sandbox's reach:
    a board at the old default path moves to the configured one, queued records leave the
    old shared spool, and the old Codex grants are taken back. Refused while a swarm
    job is active here (old and new hooks would overlap, agents write the board) unless forced;
    never moves the running plugin. cfg: the swarm config in use (default: the default config)."""
    import json
    import time
    from swarm import cli
    settings_path = settings_path or cli.claude_settings_path()
    skills_dir = skills_dir or paths.home() / ".claude" / "skills"
    if cfg is None:
        cfg = cli.load_config(paths.config_path())
    if marker_dir is None:
        marker_dir = marker_dir_of(cfg)
    codex_config_path = codex_config_path or _codex_home() / "config.toml"
    settings = {}
    if settings_path.exists() and settings_path.read_text().strip():
        settings = json.loads(settings_path.read_text())
    old_hooks = legacy_hook_commands(settings, skills_dir)
    old_dir = skills_dir / "swarm"
    move_dir = old_dir.is_dir() and old_dir.resolve() != paths.PLUGIN_ROOT.resolve()
    old_board = _old_board(cfg)
    old_spool = _old_spool_pending(cfg)
    grants = codex_config_path.exists() and _has_old_grants(codex_config_path)
    if not (old_hooks or move_dir or old_board or old_spool or grants):
        return [Step("migrate", "ok", "nothing left of the old install")]
    try:
        jobs = active_jobs(marker_dir)
    except UnsafeMarkerDir as exc:
        if not force:
            return [Step("migrate", "refused", f"unsafe marker dir: {exc}. Whether a job runs here can't be "
                         f"told; see `swarm doctor`, fix it, then run `swarm migrate`")]
        jobs = []
    if jobs and not force:
        return [Step("migrate", "refused", f"swarm job(s) active on this machine ({', '.join(jobs)}): old and "
                     f"new hooks would overlap, and agents write the board. Run `swarm migrate` when none is, "
                     f"or `swarm migrate --force`")]
    steps = []
    if old_hooks:
        from swarm import safefile
        safefile.backup(settings_path)
        safefile.write_preserving(settings_path, json.dumps(_without_swarm_hooks(settings, skills_dir), indent=2) + "\n")
        steps.append(Step("migrate", "changed", f"removed {len(old_hooks)} old swarm hook(s) from {settings_path} "
                          f"(backup next to it)"))
    if move_dir:
        target = dest or paths.share_dir() / f"legacy-skill-{time.strftime('%Y%m%d-%H%M%S')}"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(old_dir), str(target))
        steps.append(Step("migrate", "changed", f"moved {old_dir} to {target} (delete it once `swarm doctor` is clean)"))
    if old_board:
        steps.append(_move_board(board_backend(cfg), *old_board))
    if old_spool:
        steps += _move_old_spool(cfg)
    if grants:
        from swarm import codex_config
        status, detail = codex_config.remove_old_grants(codex_config_path)
        if status != "ok":
            steps.append(Step("migrate", status, detail))
    return steps or [Step("migrate", "ok", "nothing left of the old install")]


def _has_old_grants(path: Path) -> bool:
    import tomllib
    from swarm import codex_config
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return False
    old = {str(codex_config._absolute(paths.state_dir())), str(codex_config._absolute(OLD_SPOOL))}
    return any(str(codex_config._absolute(r)) in old for _, r in _codex_roots({"sandbox_workspace_write":
               data.get("sandbox_workspace_write")}))


def _move_board(backend: str, old: Path, new: Path) -> Step:
    """Move a local board from the old default to `new` under the board's own lock: the file
    board's directory is renamed while its `lock` is held; the SQLite database is copied with the
    backup API while a write transaction holds off every writer, then the old files are renamed
    to <name>.migrated-<time> (kept, never deleted). Nothing is followed: a link at the old path,
    or dirs that aren't this user's, are refused."""
    import time
    if os.path.lexists(new):
        return Step("migrate", "manual", f"a board exists both at the old default {old} and at {new}: left both "
                                         f"alone; keep one (move or delete the other by hand)")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    try:
        od = safefs.open_base(old.parent, create=False)
    except (OSError, ValueError) as exc:
        return Step("migrate", "manual", f"can't open {old.parent} safely ({_error_kind(exc)}): move {old} to {new} by hand")
    try:
        nd = safefs.open_base(new.parent, create=True)
    except (OSError, ValueError) as exc:
        os.close(od)
        return Step("migrate", "manual", f"can't open {new.parent} safely ({_error_kind(exc)}): move {old} to {new} by hand")
    try:
        import stat as _stat
        st = compat.stat(old.name, dir_fd=od, follow_symlinks=False)
        want = _stat.S_ISDIR if backend == "file" else _stat.S_ISREG
        if not want(st.st_mode) or st.st_uid != compat.uid():
            return Step("migrate", "manual", f"{old} is not a plain {'directory' if backend == 'file' else 'file'} "
                                             f"of yours (a link?): left alone; move the board by hand if it is yours")
        if backend == "file":
            bd = safefs.open_sub(od, old.name, create=False)
            try:
                with safefs.locked(bd, "lock"):
                    compat.rename(old.name, new.name, src_dir_fd=od, dst_dir_fd=nd)
            finally:
                os.close(bd)
        else:
            _move_sqlite(old, new, od, nd, stamp, (st.st_dev, st.st_ino))
    except OSError as exc:
        return Step("migrate", "manual", f"couldn't move the board {old} to {new} ({_error_kind(exc)}): move it by hand")
    finally:
        os.close(od)
        os.close(nd)
    return Step("migrate", "changed", f"moved the {backend} board {old} to {new}, out of the sandbox's reach"
                + (f" (the old files are kept as {old.name}.migrated-{stamp}*)" if backend == "sqlite" else ""))


def _move_sqlite(old: Path, new: Path, od: int, nd: int, stamp: str, ident: tuple) -> None:
    """ident: (st_dev, st_ino) of the checked old file. SQLite opens by path, so the entry is
    checked again once the copy is made: a file swapped for a link (or another file) meanwhile
    aborts the move, and the copy is discarded."""
    import sqlite3

    def same() -> bool:
        import stat as _stat
        try:
            s = compat.stat(old.name, dir_fd=od, follow_symlinks=False)
        except OSError:
            return False
        return _stat.S_ISREG(s.st_mode) and (s.st_dev, s.st_ino) == ident

    def via(fd: int, name: str, plain: Path) -> str:     # the verified dir fd where /proc has it
        return f"/proc/self/fd/{fd}/{name}" if os.path.isdir("/proc/self/fd") else str(plain)
    lock = sqlite3.connect(via(od, old.name, old), isolation_level=None, timeout=30)
    try:
        lock.execute("BEGIN IMMEDIATE")                    # every writer waits from here on
        tmp = f".{new.name}.migrating-{os.getpid()}"
        src = sqlite3.connect(via(od, old.name, old), timeout=30)
        dst = sqlite3.connect(via(nd, tmp, new.parent / tmp))
        try:
            if not same():
                raise PermissionError(f"{old} changed while being moved")
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        if not same():
            compat.unlink(tmp, dir_fd=nd)
            raise PermissionError(f"{old} changed while being moved")
        for suffix in ("-wal", "-shm", "-journal"):
            try:
                compat.unlink(tmp + suffix, dir_fd=nd)
            except FileNotFoundError:
                pass
        compat.chmod(tmp, 0o600, dir_fd=nd)
        compat.rename(tmp, new.name, src_dir_fd=nd, dst_dir_fd=nd)
        for suffix in ("", "-wal", "-shm", "-journal"):
            try:
                compat.rename(old.name + suffix, f"{old.name}.migrated-{stamp}{suffix}", src_dir_fd=od, dst_dir_fd=od)
            except FileNotFoundError:
                pass
    finally:
        try:
            lock.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        lock.close()


SPOOL_RECORD = r"[A-Za-z0-9_-]{1,128}\.(json|mem|vrd|stuck)"


def _move_old_spool(cfg: dict) -> list[Step]:
    """Queued records of the old shared spool (/tmp/claude/swarm-spool) into the configured
    per-user one, only when this user owns the old dir and every dir above it is root's or this
    user's (safefs.open_base); each record read with safefs (no link, FIFO or hard link, this
    user's), written atomically, then removed. [] when there is nothing of this user's to move."""
    import re
    from swarm import codex_config
    try:
        od = safefs.open_base(Path(OLD_SPOOL), create=False)
    except (OSError, ValueError):
        return []
    moved = skipped = 0
    try:
        names = [n for n in sorted(compat.listdir(od)) if re.fullmatch(SPOOL_RECORD, n)]
        if not names:
            return []
        try:
            nd = safefs.open_base(codex_config.spool_path(cfg), create=True)
        except (OSError, ValueError) as exc:
            return [Step("migrate", "manual", f"can't open the spool dir {codex_config.spool_path(cfg)} safely "
                                              f"({_error_kind(exc)}); {len(names)} queued record(s) left in {OLD_SPOOL}")]
        try:
            for n in names:
                data = safefs.read(od, n)
                if data is None or safefs.exists(nd, n):
                    skipped += 1
                    continue
                safefs.write_atomic(nd, n, data)
                safefs.unlink(od, n)
                moved += 1
        finally:
            os.close(nd)
    finally:
        os.close(od)
    detail = f"moved {moved} queued record(s) from the old shared spool {OLD_SPOOL} to {codex_config.spool_path(cfg)}"
    if skipped:
        detail += f"; left {skipped} that weren't plain files of yours"
    return [Step("migrate", "changed" if not skipped else "manual", detail)]


def supervisor_step(cfg: dict, config: Path, run=None) -> Step:
    """Install the user timer when [supervise] is enabled. Skipped on Windows: the supervisor is
    Linux only (systemd user timers)."""
    if paths.IS_WINDOWS:
        return Step("supervisor", "skipped", "not available on Windows (needs a systemd user timer)")
    import subprocess
    from swarm.supervisor import settings as st, systemd
    try:
        sup = st.settings(cfg)
    except st.SettingsError as exc:
        return Step("supervisor", "failed", str(exc))
    if not sup["enabled"]:
        return Step("supervisor", "skipped", "off ([supervise] enabled = false)")
    return systemd.install(config, sup["timer_minutes"], run=run or subprocess.run)


def bootstrap(host: str | None, *, config: Path | None = None, stamp: Path | None = None) -> list[Step]:
    from swarm.cli import load_config
    config = config or paths.config_path()
    steps = [Step("venv", "ok", os.environ.get("VIRTUAL_ENV") or str(paths.venv_dir()))]
    tight = tighten_local_dirs()   # first: safefs refuses paths through loose ~/.local dirs
    if tight:
        steps.append(tight)
    steps.append(ensure_launcher())
    try:   # once per bootstrap (a new plugin version): bytecode of versions that are gone
        prune_pycache()
    except Exception:
        pass
    try:
        steps.append(ensure_config(config))
        chan = os.environ.get("SWARM_CHANNEL")   # install.sh/install.ps1 --channel: what `swarm upgrade` follows
        if chan in channel.CHANNELS and channel.read_channel(config) != chan and channel.write_channel(chan, config):
            steps.append(Step("channel", "changed", f"{config}: [upgrade] channel = \"{chan}\""))
        cfg = load_config(config)
        tight = tighten_local_dirs(cfg)   # the configured marker/spool dirs, if not the defaults
        if tight:
            steps.append(tight)
    except Exception as exc:   # unreadable or not TOML: a failed step (no stamp, a notice), never a traceback
        steps.append(Step("config", "failed", f"can't read {config}: {_error_kind(exc)}; fix it (it is the swarm's "
                          f"config), then start a new session or run `swarm bootstrap`"))
        for name in ("board", "host", "supervisor", "migrate"):     # each needs the config
            steps.append(Step(name, "skipped", "fix the config first"))
        write_notices(host, steps)
        return steps
    # by name, never by position: a "local dirs" step may follow the config step
    config_step = next(s for s in steps if s.name == "config")
    board_step = (ensure_board(cfg) if config_step.status in ("ok", "changed") else
                  Step("board", "skipped", "fix the config first"))
    steps.append(board_step)
    steps.append(host_setup(host, cfg))
    if board_step.status in ("ok", "changed"):   # the board is usable: prune old enrolment records too (TZ concern 4)
        _prune_enrolments(cfg)
    steps.append(supervisor_step(cfg, config))
    if os.environ.get("SWARM_NO_MIGRATE") == "1":
        steps.append(Step("migrate", "skipped", "SWARM_NO_MIGRATE set"))
    else:
        try:
            steps += migrate(marker_dir=marker_dir_of(cfg), cfg=cfg)
        except Exception as exc:
            steps.append(Step("migrate", "failed", type(exc).__name__))
    write_notices(host, steps)
    if stamp is not None and not any(s.status in ("failed", "refused") for s in steps):
        _touch_stamp(stamp)
    return steps


def _prune_enrolments(cfg: dict) -> None:
    """supervisor.command.prune_enrolments on this board (records of closed jobs older than the
    retention window): best effort, silent; the supervise pass does the same."""
    try:
        from swarm.board import open_board
        from swarm.supervisor.command import prune_enrolments
        with open_board(cfg) as board:
            prune_enrolments(board, cfg)
    except Exception:
        pass


def _touch_stamp(stamp: Path) -> None:
    """The "this version is set up" stamp bin/swarm-hook passes (in the private host dir): its
    dir opened without following links, the file created or touched by safefs (a planted link,
    FIFO or hard link there is refused, never followed). Best effort: no stamp means bootstrap
    runs again at the next session start."""
    strict = 0o700 if stamp.parent == paths.host_dir() else None
    try:
        d = safefs.open_base(stamp.parent, strict_mode=strict)
    except (OSError, ValueError):
        return
    try:
        safefs.touch(d, stamp.name)
    except (OSError, ValueError):
        pass
    finally:
        os.close(d)


# ------------------------------------------------------------------------- notices
#
# What the detached bootstrap couldn't finish alone, for the user: stored as data (step name,
# status, detail) in the private host dir, never as ready hook output. What reaches a session is
# built by hook_output() from a fixed template, with every step re-validated: a known step name,
# a to-do status, and a detail of printable ASCII only, bounded in size.

NOTICE_STEPS = ("venv", "local dirs", "launcher", "config", "board", "host", "supervisor", "migrate")
NOTICE_STATUSES = ("manual", "failed", "refused")
NOTICE_LINE_MAX = 400        # one line of a step's detail (the longest fixed step text fits)
NOTICE_LINES_MAX = 8
NOTICE_MAX_BYTES = 64 * 1024
NOTICE_TAIL = "\nTell the user about these steps before starting a swarm."


def _notice_host(host: str | None) -> str | None:
    import re
    h = host or "cli"
    return h if re.fullmatch(r"[a-z]{1,16}", h) else None


def notices_path(host: str | None) -> Path:
    return paths.host_dir() / f"notices-{_notice_host(host) or 'unknown'}.json"


def _clean_detail(detail) -> str:
    if not isinstance(detail, str):
        return ""
    lines = []
    for line in detail.split("\n")[:NOTICE_LINES_MAX]:
        line = "".join(c if " " <= c <= "~" else "?" for c in line)
        lines.append(line if len(line) <= NOTICE_LINE_MAX else line[:NOTICE_LINE_MAX - 3] + "...")
    return "\n".join(lines)


def _clean_steps(data) -> list[tuple[str, str]]:
    """(name, detail) of the valid to-do steps in a stored notice; [] for anything else."""
    if not isinstance(data, dict) or data.get("v") != 1 or not isinstance(data.get("steps"), list):
        return []
    out = []
    for s in data["steps"][:len(NOTICE_STEPS) * 2]:
        if isinstance(s, dict) and s.get("name") in NOTICE_STEPS and s.get("status") in NOTICE_STATUSES:
            out.append((s["name"], _clean_detail(s.get("detail"))))
    return out


def _notice_text(steps: list[tuple[str, str]]) -> str:
    return "[swarm] setup needs you:\n" + "\n".join(f"- {n}: {d}" for n, d in steps)


def write_notices(host: str | None, steps: list[Step]) -> None:
    """Store the manual/failed/refused steps for the user (the next session start shows them
    through hook_output, the next CLI command through take_notices; whichever comes first
    consumes them). The details hold paths and the swarm's own settings only, never secrets.
    Best effort: an unsafe host dir means no notice, never a write through a link."""
    todo = [{"name": s.name, "status": s.status, "detail": s.detail}
            for s in steps if s.status in NOTICE_STATUSES]
    name = notices_path(host).name
    try:
        d = _host_fd()
    except (OSError, ValueError):
        return
    try:
        if todo:
            safefs.write_atomic(d, name, json.dumps({"v": 1, "host": host or "cli", "steps": todo}) + "\n")
        else:
            safefs.unlink(d, name)
    except OSError:
        pass
    finally:
        os.close(d)


def _take(d: int, name: str) -> list[tuple[str, str]]:
    """The valid steps of one stored notice, consumed (renamed to <name>.shown)."""
    raw = safefs.read(d, name, limit=NOTICE_MAX_BYTES)
    if raw is None:
        return []
    try:
        compat.rename(name, name + ".shown", src_dir_fd=d, dst_dir_fd=d)
    except OSError:
        return []                    # another reader took it first
    try:
        return _clean_steps(json.loads(raw))
    except ValueError:
        return []


HOST_DIR_PROBLEM = ("[swarm] setup problem: the swarm's host-only directory ~/.local/share/swarm/host can't "
                    "be used ({why}). The swarm hooks do nothing until it is fixed: run `swarm bootstrap` "
                    "(it takes group/world write off ~/.local dirs you own) and see `swarm doctor`.")


def _ascii_line(s: str, limit: int) -> str:
    """`s` as one line of printable ASCII (anything else becomes ?), at most `limit` characters."""
    return "".join(c if " " <= c <= "~" else "?" for c in s)[:limit]


STATE_DIRS = (".local/state", ".local/state/swarm")   # what bin/swarm-hook's LOOSE test looks at below share
STATE_DIR_PROBLEM = ("[swarm] setup problem: {dirs} group- or world-writable, so the swarm refuses its "
                     "markers, spool and state files there and its hooks go quiet. Run `swarm bootstrap` "
                     "(it takes group/world write off ~/.local dirs you own) and see `swarm doctor`.")


def _loose_state_dirs() -> list[str]:
    """Which of STATE_DIRS is a real directory (not followed if a link) that is group- or
    world-writable without the sticky bit: "~/<rel>" each."""
    import stat as _stat
    out = []
    for rel in STATE_DIRS:
        try:
            st = os.lstat(paths.home() / rel)
        except OSError:
            continue
        if not paths.IS_WINDOWS and _stat.S_ISDIR(st.st_mode) and st.st_mode & 0o022 and not st.st_mode & _stat.S_ISVTX:
            out.append(f"~/{rel}")
    return out


def hook_output(host: str | None) -> str | None:
    """For `swarm notices --hook-output` (bin/swarm-hook's session-start): the SessionStart hook
    output for `host`'s pending notice, consumed, as one JSON line; None when there is none.
    A loose ~/.local/state (or its swarm dir) is reported first. Only the templates below and the
    re-validated steps reach the session."""
    if _notice_host(host) is None:
        return None
    try:
        d = _host_fd(create=False)
    except FileNotFoundError:
        return None   # not set up yet: bootstrap is on its way
    except (OSError, ValueError) as exc:   # unusable: doctor FAILs it; say so
        text = HOST_DIR_PROBLEM.format(why=_ascii_line(str(exc), 240))
        return json.dumps({"systemMessage": text,
                           "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}})
    try:
        steps = _take(d, notices_path(host).name)
    finally:
        os.close(d)
    loose = _loose_state_dirs()
    problem = (STATE_DIR_PROBLEM.format(dirs=" and ".join(loose) + (" are" if len(loose) > 1 else " is"))
               if loose else "")
    if not steps and not problem:
        return None
    text = "\n".join(t for t in (problem, _notice_text(steps) if steps else "") if t)
    return json.dumps({"systemMessage": text,
                       "hookSpecificOutput": {"hookEventName": "SessionStart",
                                              "additionalContext": text + (NOTICE_TAIL if steps else "")}})


def take_notices() -> str | None:
    """For the CLI: the pending notices' text (all hosts), consumed."""
    import re
    try:
        d = _host_fd(create=False)
    except (OSError, ValueError):
        return None
    texts = []
    try:
        for name in sorted(compat.listdir(d)):
            if re.fullmatch(r"notices-[a-z]{1,16}\.json", name):
                steps = _take(d, name)
                if steps:
                    texts.append(_notice_text(steps))
    except OSError:
        pass
    finally:
        os.close(d)
    return "\n".join(texts) or None


# --------------------------------------------------------------------------- doctor

@dataclass(frozen=True)
class Check:
    name: str
    ok: bool | None          # None: warning / can't tell
    detail: str
    fix: str = ""


_CHECK_COLORS = {"OK": 32, "FAIL": 31, "WARN": 33}   # ok green, FAIL red, WARN yellow


def format_checks(checks: list[Check], color: bool = False) -> str:
    """Plain text is byte-identical whether or not `color` is passed (existing callers/tests
    that never pass it keep working unchanged). `color=True` wraps the status word in the same
    SGR codes install.sh uses, and bolds the check name; reuses cli._sgr/_bold rather than a
    second copy of the escape codes."""
    from swarm.cli import _bold, _sgr
    word = {True: "OK", False: "FAIL", None: "WARN"}
    lines = []
    for c in checks:
        w = word[c.ok]
        wtxt = f"{w:<5}"
        if color:
            wtxt = _sgr(_CHECK_COLORS[w], wtxt)
        name = _bold(f"{c.name:<20}", color)
        lines.append(f"{wtxt}{name}{c.detail}")
        if c.fix and c.ok is not True:
            lines.append(f"{'':<25}fix: {c.fix}")
    return "\n".join(lines)


def supervisor_checks(cfg: dict, host: str | None, run=None, which=None) -> list[Check]:
    if paths.IS_WINDOWS:
        return [Check("supervise", True, "not available on Windows (Linux systemd only)")]
    import datetime as dt
    import subprocess
    from swarm.supervisor import settings as st, systemd
    run, which = run or subprocess.run, which or shutil.which
    try:
        sup = st.settings(cfg)
    except st.SettingsError as exc:
        return [Check("supervise config", False, str(exc), "fix [supervise] in the swarm config")]
    if not sup["enabled"]:
        return [Check("supervise", True, "off (opt in: `swarm supervise enable`)")]
    out = [Check("supervise config", True, "valid")]
    if st.enabled_implied(cfg):
        out.append(Check("supervise choice", None, "on because this config predates the opt-in (it has no "
                         "[supervise] enabled); make it explicit", "swarm supervise enable"))
    off_file = st.switched_off()
    if off_file is not None:
        out.append(Check("supervise off file", None, f"switched off by {off_file}", f"rm {off_file}"))
    else:
        out.append(Check("supervise off file", True, "absent"))
    # One place for every supervise check (the containment check included): with no user
    # systemd manager, the timer can never be active and linger is moot (it only controls whether
    # that same manager survives logout), so one WARN here replaces what would otherwise be three
    # contradictory timer/last-run/linger lines for the same root cause.
    scope = _containment_checks(cfg)
    out += scope
    no_manager = bool(scope) and scope[0].ok is not True
    if not no_manager:
        state = systemd.timer_state(run)
        active = state.get("ActiveState") == "active" and state.get("UnitFileState") == "enabled"
        out.append(Check("supervise timer", active,
                         f"{systemd.TIMER}: {state.get('ActiveState', '?')}/{state.get('UnitFileState', '?')}",
                         f"swarm bootstrap, or systemctl --user enable --now {systemd.TIMER}"))
        last = st.load_state().get("last_run_at")
        age = None
        if last:
            try:
                age = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(last)).total_seconds() / 60
            except (TypeError, ValueError):
                pass
        fresh = age is not None and 0 <= age <= 3 * sup["timer_minutes"]
        out.append(Check("supervise last run", fresh, "never" if age is None else f"{age:.0f} min ago",
                         "journalctl --user -u swarm-supervise.service -n 50"))
        linger = systemd.linger(run)
        out.append(Check("linger", True if linger else None,
                         {True: "yes", False: "no", None: "unknown"}[linger]
                         + ("" if linger else ": user timers stop when you log out"),
                         "loginctl enable-linger $USER (may need root)"))
    exposed = st.codex_exposure()
    out.append(Check("supervise private dir", not exposed,
                     f"{st.private_dir()}: " + (f"covered by Codex writable_roots {', '.join(map(str, exposed))}"
                                                if exposed else "under no Codex writable root in the Codex config")
                     + "; also never start Codex with its working root at $HOME",
                     "remove those entries from [sandbox_workspace_write] writable_roots in the Codex config"))
    wds = [Path(os.path.expanduser(w)) for w in sup["allowed_workdirs"]]
    out.append(Check("supervise workdirs", True if any(w.is_dir() for w in wds) else None,
                     "allowed_workdirs: " + ", ".join(sup["allowed_workdirs"])
                     + ("" if any(w.is_dir() for w in wds) else " (none exists: every restart will be refused)"),
                     "set [supervise] allowed_workdirs to the directories your jobs run in"))
    path = systemd.service_path()
    found = {h: which(sup[f"{h}_bin"], path=path) for h in ("claude", "codex")}
    # The harness of the user running doctor: --host (or the session doctor runs in), else
    # the harnesses whose hooks ran for this user, else whichever is installed
    hs = [host] if host else [h for h in ("claude", "codex") if _hooks_ran(h)] or \
        [h for h in ("claude", "codex") if found[h]]
    if not hs:
        out.append(Check("supervise harness", None,
                         f"{sup['claude_bin']}, {sup['codex_bin']}: neither claude nor codex is on PATH; "
                         "auto-restart inactive until harness installed",
                         "install the harness this user runs agents with, or set [supervise] "
                         "claude_bin / codex_bin"))
    for h in hs:
        out.append(Check("supervise harness" if len(hs) == 1 else f"supervise harness ({h})", True if found[h] else None,
                         f"{h}: {sup[f'{h}_bin']}: {found[h] or 'not on PATH'}"
                         + ("" if found[h] else "; auto-restart inactive until harness installed"),
                         f"install {h} or set [supervise] {h}_bin"))
    if "codex" in hs:
        out.append(Check("supervise codex hooks", None,
                         "can't tell whether the plugin's hooks are trusted; replacements that never join are "
                         "killed after enrol_minutes",
                         "open /hooks in a Codex session and trust the swarm plugin's hooks"))
    return out


def _error_kind(exc: BaseException) -> str:
    """What went wrong reading a user's file, without its content: the OS error, or the parse
    error's kind and position."""
    import re
    if isinstance(exc, OSError):
        return exc.strerror or type(exc).__name__
    if isinstance(exc, json.JSONDecodeError):
        return f"invalid JSON at line {exc.lineno} column {exc.colno}"
    m = re.search(r"\(at line (\d+), column (\d+)\)", str(exc))   # tomllib's position suffix
    kind = "invalid TOML" if type(exc).__name__ == "TOMLDecodeError" else type(exc).__name__
    return f"{kind} at line {m.group(1)} column {m.group(2)}" if m else kind


def _claude_plugin_root(plugins_dir: Path) -> Path | None:
    """The swarm plugin's install path from Claude Code's installed_plugins.json (keys are
    <plugin>@<marketplace>, each a list of installs with installPath), or None."""
    import json
    try:
        data = json.loads((plugins_dir / "installed_plugins.json").read_text())
    except (OSError, ValueError):
        return None
    for key, installs in (data.get("plugins") or {}).items():
        if key.split("@")[0] == "swarm" and installs:
            return Path(installs[-1].get("installPath", ""))
    return None


def _legacy_database_check(config: Path, cfg: dict) -> list[Check]:
    """WARN when [database] leaves out user or dbname on Postgres: the config still gets the
    pre-rename default (LEGACY_DATABASE_DEFAULTS), not the current one."""
    import tomllib
    from swarm.cli import LEGACY_DATABASE_DEFAULTS, implicit_legacy_database_keys
    if cfg.get("board", {}).get("backend", "postgres") != "postgres":
        return []
    try:
        fd = safefs.open_base(config.parent, create=False)
        try:
            keys = implicit_legacy_database_keys(tomllib.loads(safefs.read_text(fd, config.name) or ""))
        finally:
            os.close(fd)
    except (OSError, ValueError):
        return []
    if not keys:
        return []
    detail = "[database] has no " + " and no ".join(keys) + "; using the old default " + \
        ", ".join(f'{k} = "{LEGACY_DATABASE_DEFAULTS[k]}"' for k in keys)
    return [Check("database defaults", None, detail,
                  f"set {' and '.join(keys)} explicitly in [database] of {config}")]


def _common_checks(config: Path) -> list[Check]:
    """Checks every host shares. Details hold paths, versions and error type names only: never a
    config value or a secret."""
    import subprocess
    from swarm.cli import load_config
    out = []
    v = paths.venv_dir()
    if paths.IS_WINDOWS:   # bin/swarm.cmd's stamp: a CRC-32 of requirements.txt (winlaunch)
        from swarm import winlaunch
        req = winlaunch.requirements_stamp(paths.PLUGIN_ROOT)
    else:
        req = subprocess.run(["sh", "-c", f"cksum < '{paths.PLUGIN_ROOT / 'requirements.txt'}' | cut -d' ' -f1"],
                             capture_output=True, text=True).stdout.strip()
    have = (v / ".swarm-requirements").read_text().strip() if (v / ".swarm-requirements").exists() else ""
    out.append(Check("venv", paths.venv_python(v).exists() and have == req, str(v),
                     f"run `{paths.launcher_path()} --help` (the launcher rebuilds it)"))
    lp = paths.launcher_path()
    target = launcher_target(lp) if lp.exists() else None
    out.append(Check("launcher", bool(target and (target / "bin/swarm").exists()),
                     f"{lp} -> {target}" if target else f"{lp} missing",
                     f"{paths.PLUGIN_ROOT}/bin/swarm bootstrap"))
    cfg = None
    try:
        if config.exists():
            c = ensure_config(config)
        else:   # no config is fine: every key has a default (the file board), bootstrap writes one
            c = Step("config", "ok", f"{config} not created yet: the defaults apply (file board)")
        if c.status == "ok":
            cfg = load_config(config)
    except Exception as exc:   # unreadable or not TOML: say where, never the content; the rest goes on
        c = Step("config", "failed", f"can't read {config}: {_error_kind(exc)}")
    out.append(Check("config", c.status == "ok", c.detail, f"edit {config} ({FILL_IN})"))
    if cfg is not None and cfg["board"].get("backend_implied"):
        out.append(Check("board backend", None,
                         "no [board] backend is set: [database] is present, so the board stays on postgres "
                         "(the default is now \"file\")",
                         f'add backend = "postgres" under [board] in {config} to make it explicit'))
    if cfg is not None:
        out += _legacy_database_check(config, cfg)
        from swarm.board import SCHEMA_VERSION, backend_class
        try:
            ver = backend_class(cfg).schema_version(cfg)
            out.append(Check("board", ver == SCHEMA_VERSION, f"schema {ver}, this plugin {SCHEMA_VERSION}",
                             "~/.local/bin/swarm status (sets the schema up)"))
        except Exception as exc:
            out.append(Check("board", False, f"unreachable ({type(exc.__cause__ or exc).__name__})",
                             f"`~/.local/bin/swarm status` shows the error; check [database] host, port, user, "
                             f"dbname and password_env_file (or [sqlite]/[file] path) in {config}, and that "
                             f"the server is up"))
    return out


def _containment_checks(cfg: dict) -> list[Check]:
    """With the supervisor on: whether replacement sessions get their own systemd scope, or the
    process-group fallback that can't contain a descendant clearing its environment."""
    from swarm.supervisor.settings import SettingsError, settings
    try:
        if not settings(cfg)["enabled"]:
            return []
    except SettingsError:
        return []
    from swarm.supervisor.runner import scope_available
    if scope_available():
        return [Check("replacement scope", True, "systemd user scopes (systemd-run --user --scope)")]
    return [Check("replacement scope", None,
                  "no systemd user manager: auto-restart inactive until loginctl enable-linger "
                  "$USER (each replacement runs in its own systemd scope)",
                  "loginctl enable-linger $USER, so the user manager (and the supervisor timer) always runs")]


def _claude_checks(plugins_dir: Path) -> list[Check]:
    import json
    from swarm import cli
    out = []
    root = _claude_plugin_root(plugins_dir)
    installed = root is not None and root.exists()
    out.append(Check("plugin", installed, f"claude: {root}" if root else "claude: not installed",
                     "in Claude Code: /plugin marketplace add https://github.com/fcarucci/Swarm.git, "
                     "then /plugin install swarm@swarm"))
    if root is not None:
        try:
            events = set(json.loads((root / "hooks/hooks.json").read_text())["hooks"])
        except (OSError, ValueError, KeyError):
            events = set()
        want = {"SessionStart", "SubagentStart", "PreToolUse", "PostToolUse", "SubagentStop"}
        out.append(Check("hooks", want <= events, f"claude: {', '.join(sorted(events)) or 'none'}",
                         "reinstall: /plugin uninstall swarm@swarm, then /plugin install swarm@swarm"))
    sp = cli.claude_settings_path()
    settings, unreadable = {}, None
    try:
        if sp.exists():                          # only a missing file means "no settings"
            text = sp.read_text()
            if not text.strip():
                raise ValueError("empty or invalid JSON")
            settings = json.loads(text)
        if not isinstance(settings, dict):
            raise ValueError("not a JSON object")
    except Exception as exc:
        settings, unreadable = {}, (exc.args[0] if type(exc) is ValueError else _error_kind(exc))
    if unreadable:
        out.append(Check("old hooks", None, f"can't read {sp}: {unreadable}",
                         f"make {sp} readable and valid JSON (Claude Code's user settings), then rerun doctor"))
    else:
        old = legacy_hook_commands(settings)          # the same matcher as migrate
        # leftovers are a FAIL while the plugin is installed too (both would run); a warning otherwise
        out.append(Check("old hooks", True if not old else (False if installed else None),
                         f"{len(old)} swarm hook(s) in {sp}" if old else "none",
                         "~/.local/bin/swarm migrate (when no swarm job is active)"))
    legacy = paths.home() / ".claude/skills/swarm"
    out.append(Check("old skill dir", not legacy.exists(), str(legacy) if legacy.exists() else "none",
                     "~/.local/bin/swarm migrate"))
    model = settings.get("model")
    out.append(Check("orchestrator model", None, "claude: can't tell (settings unreadable)" if unreadable
                     else f"claude: {model or 'host default'} (not set by the swarm)"))
    return out


def doctor(host: str | None = None, *, config: Path | None = None, claude_plugins: Path | None = None,
           env=os.environ) -> list[Check]:
    """What is off in this machine's swarm setup, each with its fix. env: the process environment
    (a Codex thread id there means doctor runs inside a Codex session: the sandbox probe runs)."""
    config = config or paths.config_path()
    checks = _common_checks(config)
    if host in (None, "claude"):
        checks += _claude_checks(claude_plugins or paths.home() / ".claude/plugins")
    if host == "codex":
        from swarm.cli import load_config
        try:
            cfg = load_config(config)
        except Exception:          # already a config FAIL above: check Codex against the defaults
            cfg = load_config(Path("/nonexistent/swarm-config.toml"))
        checks += _codex_checks(cfg, env)
    from swarm.cli import load_config
    try:
        cfg, cfg_ok = load_config(config), True
    except Exception:          # already a config FAIL above
        cfg, cfg_ok = load_config(Path("/nonexistent/swarm-config.toml")), False
    checks += exposure_checks(cfg)
    if cfg_ok:
        checks += _transcripts_users_check(cfg)
        checks += _capture_failed_check(cfg)
        checks += _bg_orphans_check(cfg)
    checks += supervisor_checks(cfg, host)
    return checks


# ------------------------------------------------------------- doctor: what a sandbox may write

def _codex_roots(data: dict) -> list[tuple[str, str]]:
    """(where, root) for every writable root in a parsed Codex config: the base table and each
    legacy [profiles.<name>] table."""
    out = []

    def roots_of(table, where):
        sw = table.get("sandbox_workspace_write") if isinstance(table, dict) else None
        rs = sw.get("writable_roots") if isinstance(sw, dict) else None
        for r in rs if isinstance(rs, list) else []:
            if isinstance(r, str):
                out.append((where, r))
    roots_of(data, "config.toml")
    profiles = data.get("profiles")
    for name, prof in (profiles.items() if isinstance(profiles, dict) else []):
        roots_of(prof, f"config.toml [profiles.{name}]")
    return out


def writable_roots() -> list[tuple[str, Path]]:
    """Every directory a sandboxed agent of this user may be granted, read (never written) from
    the Codex config (base table, legacy profiles, <name>.config.toml profile files) and from
    Claude Code's sandbox.filesystem.allowWrite. (where it is granted, absolute path)."""
    import tomllib
    from swarm import cli
    out = []
    home = _codex_home()
    files = [home / "config.toml"] + sorted(home.glob("*.config.toml"))
    for f in files:
        try:
            data = tomllib.loads(f.read_text())
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
            continue
        for where, r in _codex_roots(data):
            where = where.replace("config.toml", f.name, 1) if f.name != "config.toml" else where
            out.append((f"Codex {home / where}", Path(os.path.abspath(os.path.expanduser(r)))))
    sp = cli.claude_settings_path()
    try:
        s = json.loads(sp.read_text())
        allow = s["sandbox"]["filesystem"]["allowWrite"]
    except (OSError, ValueError, KeyError, TypeError):
        allow = []
    for r in allow if isinstance(allow, list) else []:
        if isinstance(r, str):
            out.append((f"Claude {sp} sandbox.filesystem.allowWrite",
                        Path(os.path.abspath(os.path.expanduser(r)))))
    return out


def _within(p: Path, root: Path) -> bool:
    return p == root or root in p.parents


def exposure_checks(cfg: dict) -> list[Check]:
    """FAILs for what a sandboxed agent must never write and for loose
    ~/.local dirs (safefs refuses them), and WARNs for a base-table network grant and a DB on a
    Unix socket. Details name paths and the swarm's own settings only."""
    import stat as _stat
    from swarm import codex_config
    out = []
    roots = writable_roots()
    state, share = paths.state_dir(), paths.share_dir()
    bad = [(w, r) for w, r in roots
           if _within(state, r) or _within(share, r) or _within(r, share)]
    out.append(Check("sandbox roots", not bad,
                     "; ".join(f"{r} (in {w})" for w, r in bad) + ": a sandboxed agent could write host-only "
                     f"files ({state}, or {share}: the venv, host/ and supervisor/)" if bad else
                     f"no sandbox may write {state} or {share}",
                     "remove those entries from writable_roots in the Codex config (or from "
                     "sandbox.filesystem.allowWrite in the Claude settings); the swarm needs only the spool and "
                     "marker dirs (`swarm bootstrap --host codex` takes back what an earlier swarm granted)"))
    bdir = codex_config.board_dir(cfg)
    if bdir is not None:
        under = [(w, r) for w, r in roots if _within(bdir, r)]
        opted = (cfg.get("codex") or {}).get("board_writable") is True
        out.append(Check("board location", not under,
                         (f"the {board_backend(cfg)} board {bdir} is under {', '.join(str(r) for _, r in under)}"
                          f" ({', '.join(w for w, _ in under)}): a sandboxed agent can fake and tamper with board "
                          f"rows and plant links in it" + (" ([codex] board_writable = true grants it)" if opted else ""))
                         if under else f"{bdir}: under no sandbox writable root",
                         ("set [codex] board_writable = false, then " if opted else "")
                         + f"move the board out of the sandbox's reach (default under ~/.local/share/swarm-board/; "
                           f"`swarm migrate` moves the old default), or take its dir out of the writable roots"))
    problem = spool_problem(cfg)
    out.append(Check("spool dir", problem is None,
                     problem.replace("the old shared default", "the old shared default (/tmp is shared by every "
                                     "OS user)") if problem else f"{codex_config.spool_path(cfg)}: private to you",
                     'set [board] spool_dir = "~/.local/state/swarm/spool" (or a /tmp path naming your uid, '
                     "e.g. /tmp/claude-{uid}/swarm-spool), then `swarm bootstrap` and `swarm migrate`"))
    loose = []
    for d in (paths.home() / ".local", paths.home() / ".local/share", paths.home() / ".local/state",
              share, state, paths.host_dir()):
        try:
            st = os.lstat(d)
        except OSError:
            continue
        if not paths.IS_WINDOWS and _stat.S_ISDIR(st.st_mode) and st.st_mode & 0o022 and not st.st_mode & _stat.S_ISVTX:
            loose.append(str(d))
    out.append(Check("local dirs", not loose,
                     (f"group- or world-writable: {', '.join(loose)}; the swarm refuses to keep its files "
                      f"there") if loose else "~/.local dirs are not group- or world-writable",
                     f"chmod go-w {' '.join(loose)}"))
    try:
        import tomllib
        data = tomllib.loads((_codex_home() / "config.toml").read_text())
        sw = data.get("sandbox_workspace_write")
        net = isinstance(sw, dict) and sw.get("network_access") is True
    except (OSError, UnicodeDecodeError, ValueError):
        net = None
    if net is not None:
        out.append(Check("codex network", None if net else True,
                         "network_access = true in the base [sandbox_workspace_write]: every workspace-write "
                         "Codex session, swarm or not, has outbound network (an exfiltration path)" if net
                         else "the base Codex sandbox has no network grant",
                         f"delete network_access = true from [sandbox_workspace_write] in {_codex_home() / 'config.toml'}"
                         " unless you want it for every session; swarm agents don't need it (their posts spool). "
                         "For direct board access in swarm sessions only: [codex] network_access = true in the "
                         "swarm config, then `swarm bootstrap --host codex` and `codex -p swarm`"))
    if board_backend(cfg) == "postgres":
        socks = [s for s in ("database", "watch_database")
                 if str((cfg.get(s) or {}).get("host") or "").startswith("/")]
        if socks:
            out.append(Check("db socket", None,
                             f"[{'] and ['.join(socks)}] host is a Unix socket directory: the Codex Linux sandbox may "
                             "allow Unix socket connects (unverified), so a sandboxed agent that can read the "
                             "password file could reach the board directly",
                             "use a TCP host (e.g. localhost) for [database] host"))
    return out


def _transcripts_users_check(cfg: dict) -> list[Check]:
    """With transcripts or memory provenance on (provenance is on by default), a board
    holding agents of more than one OS user shares them through one database role: until a later
    release's per-user roles, each user can read (and alter) the other's board rows, archived transcripts
    and memory excerpts included."""
    from swarm import provenance
    transcripts_on = bool((cfg.get("transcripts") or {}).get("enabled"))
    excerpts_on = provenance.enabled(cfg)
    if not (transcripts_on or excerpts_on):
        return []
    from swarm.board import open_board
    users = set()
    try:
        with open_board(cfg, init_timeout=5.0, readers=True) as b:
            for js in b.jobs(True):
                users |= {a.os_user for a in b.agents(js.job) if a.os_user}
    except Exception:
        return []                     # the board check above reports an unreachable board
    on = " and ".join(w for w, v in (("transcripts", transcripts_on), ("memory provenance", excerpts_on)) if v)
    knobs = " and ".join(k for k, v in (("[transcripts] enabled", transcripts_on),
                                        ("[provenance] enabled", excerpts_on)) if v)
    fix = (f"turn {knobs} off, or give each OS user its own board, if the users must not read "
           "each other's transcripts and memory excerpts")
    if len(users) <= 1:
        return [Check("transcripts users", True, f"{on} on; the board's agents are all one OS user's", fix)]
    shared = ", ".join(w for w, v in (("archived transcripts", transcripts_on), ("memory excerpts", excerpts_on)) if v)
    detail = (f"{on} on and the board has agents of {len(users)} OS users: with one shared database role "
              f"each can read and alter the other's board rows, {shared} included (per-user roles come "
              "in a later release)")
    if excerpts_on:
        detail += "; memory excerpts are readable by every OS user sharing the board role"
    return [Check("transcripts users", None, detail, fix)]


PENDING_FINAL_WARN_HOURS = 1.0   # doctor: an ended agent without a final transcript this long is shown


def _bg_orphans_check(cfg: dict) -> list[Check]:
    """Background commands of agents (swarm bg) still running on this host although their agent
    finished or their job closed. Read-only; an unreachable board is the board check's to report."""
    from swarm import bg
    from swarm.board import open_board
    try:
        with open_board(cfg, init_timeout=5.0, readers=True) as b:
            host = bg.this_host()
            here = [r for r in b.bg_orphans() if r.host == host]
    except Exception:
        return []
    if not here:
        return [Check("orphaned bg commands", True, "none on this host")]
    jobs = sorted({r.job for r in here})
    from swarm.textsafe import term_safe
    return [Check("orphaned bg commands", None,
                  term_safe(f"{len(here)} still running after their agent finished or job closed "
                            f"(jobs: {', '.join(jobs[:5])}{', ...' if len(jobs) > 5 else ''})"),
                  "~/.local/bin/swarm bg list --orphans, then ~/.local/bin/swarm bg reap "
                  "(the supervisor pass reaps them too when [supervise] is on)")]


def _capture_failed_check(cfg: dict) -> list[Check]:
    """With [transcripts] on, what is off with final transcripts: capture-failed rows
    (the agent's transcript couldn't be redacted within 3 retries of 60 s, e.g. adversarial
    output, or was too large to read), this machine+user's ended agents still without a final
    after PENDING_FINAL_WARN_HOURS (from the board: nothing pending is invisible), failed finals
    waiting for the supervisor pass, and a retry-state lock held by another process."""
    if not bool((cfg.get("transcripts") or {}).get("enabled")):
        return []
    import datetime as _dt
    import getpass
    from swarm import transcripts
    from swarm.board import open_board
    try:
        with open_board(cfg, init_timeout=5.0, readers=True) as b:
            failed = [r for r in b.transcripts() if r.failed]
            now = b.now()
            since = now - _dt.timedelta(days=float(transcripts.settings(cfg)["retention_days"]))
            recent = now - _dt.timedelta(hours=PENDING_FINAL_WARN_HOURS)
            old = 0
            for harness in ("claude", "codex"):
                args = (transcripts._host(), getpass.getuser(), harness)
                old += len(b.pending_final_transcripts(*args, since)) - len(b.pending_final_transcripts(*args, recent))
    except Exception:
        return []                     # the board check above reports an unreachable board
    from swarm.supervisor import lost
    try:
        waiting, contended = lost.slow_pending(), lost.lock_contended()
    except Exception:
        waiting, contended = 0, False
    from swarm.supervisor.settings import enabled as supervise_on
    from swarm.textsafe import term_safe
    parts = []
    if failed:
        jobs = sorted({r.job for r in failed})
        shown = ", ".join(term_safe(j) for j in jobs[:5]) + (", ..." if len(jobs) > 5 else "")
        parts.append(f"{len(failed)} final transcript capture{'s' if len(failed) != 1 else ''} failed "
                     f"(marked capture failed: the last redacted snapshot or nothing; jobs: {shown})")
    if old > 0:
        parts.append(f"{old} ended agent{'s' if old != 1 else ''} of this user without a final transcript "
                     f"for over {PENDING_FINAL_WARN_HOURS:g} h")
    if waiting:
        parts.append(f"{waiting} failed final capture{'s' if waiting != 1 else ''} waiting for the "
                     f"supervisor pass to retry" + ("" if supervise_on(cfg) else
                                                    " (it is off: [supervise] enabled, or an off file)"))
    if contended:
        parts.append(f"the transcript retry state's lock ({lost.RETRIES_LOCK} in the supervisor's private "
                     f"directory) is held by another process: retries go on without their bookkeeping")
    if not parts:
        return [Check("final transcripts", True, "no failed or overdue final captures")]
    return [Check("final transcripts", None, "; ".join(parts),
                  "`swarm status --job JOB` shows which agents; `swarm deactivate` (no time limit) or the "
                  "supervisor pass ([supervise] enabled) retries a pending one; a capture-failed row stays "
                  "as the audit record" + ("; find the lock holder with `fuser` on the lock file" if contended else ""))]


PLUGIN_LIST_CMD = "codex plugin list --json"


def _codex_plugin_listed() -> bool | None:
    """Whether Codex has the swarm plugin installed and enabled: True, False, or None (can't
    tell). `codex plugin list --json` first (its entries carry separate `installed` and `enabled`
    fields); if that flag isn't supported (non-zero exit), the human table of `codex plugin
    list`, where only an explicit installed/enabled status counts."""
    import subprocess

    def run(*args):
        try:
            return subprocess.run(["codex", "plugin", "list", *args], capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            return None
    res = run("--json")
    if res is None:
        return False                      # no codex command at all
    if res.returncode == 0:
        return plugin_json_state(res.stdout)
    res = run()
    if res is None or res.returncode != 0:
        return None
    return plugin_table_state(res.stdout)


def _is_swarm(entry: dict) -> bool:
    """A `codex plugin list --json` entry for this plugin: name "swarm", or a pluginId
    "swarm@<marketplace>" (any marketplace the user added it from)."""
    pid = entry.get("pluginId")
    return entry.get("name") == "swarm" or (isinstance(pid, str) and pid.startswith("swarm@"))


def plugin_json_state(text: str) -> bool | None:
    """From `codex plugin list --json` (codex-cli 0.157.1: {"installed": [entries], "available":
    [entries]}, each entry with pluginId, name, installed and enabled): True when a swarm entry in
    the "installed" list has installed and enabled both true; False when that list has no such
    entry (an entry under "available" is not installed, whatever its flags say); None when there
    is no list-valued "installed" key (not JSON, or a shape we don't know)."""
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("installed"), list):
        return None
    return any(isinstance(e, dict) and _is_swarm(e) and e.get("installed") is True and e.get("enabled") is True
               for e in data["installed"])


PLUGIN_OK_STATUSES = ("installed", "enabled", "installed, enabled", "installed (enabled)")


def plugin_table_state(output: str) -> bool | None:
    """Fallback, from the table of `codex plugin list` (fixture P line: "PLUGIN   STATUS   VERSION
    SOURCE", whitespace-aligned): True only for a swarm@<marketplace> row whose status is an
    explicit installed/enabled; False for any other status or no swarm row; None without the
    table header."""
    import re
    header, found = False, False
    for line in output.splitlines():
        cols = re.split(r"\s{2,}", line.strip())
        if cols[:2] == ["PLUGIN", "STATUS"]:
            header = True
            continue
        if len(cols) >= 2 and "@" in cols[0] and cols[0].split("@")[0] == "swarm":
            found = True
            if cols[1].strip().lower() in PLUGIN_OK_STATUSES:
                return True
    return False if header or found else None


def _codex_checks(cfg: dict, env=os.environ) -> list[Check]:
    import tomllib
    from swarm import codex_config
    listed = _codex_plugin_listed()
    out = [Check("plugin", listed, {True: "codex: swarm installed and enabled", False: "codex: swarm not installed "
                 "and enabled", None: f"codex: can't tell (`{PLUGIN_LIST_CMD}` gave an unknown answer)"}[listed],
                 "codex plugin marketplace add https://github.com/fcarucci/Swarm.git && codex plugin add "
                 f"swarm@swarm (check with `{PLUGIN_LIST_CMD}`; enable it if it is disabled)")]
    ran = _hooks_ran("codex")
    out.append(Check("hooks trusted", True if ran else None,
                     "codex: the swarm's SessionStart hook has run for this version" if ran
                     else "codex: no swarm hook has run since this version was installed", TRUST_STEP))
    path = _codex_home() / "config.toml"
    try:
        data = tomllib.loads(path.read_text()) if path.exists() else {}
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        out.append(Check("codex config", False, f"can't read {path}: {_error_kind(exc)}",
                         f"fix {path}, then ~/.local/bin/swarm bootstrap --host codex"))
        data = {}
    bad = codex_config._unsupported_shape(data)
    if bad:
        out.append(Check("codex config", False, f"{path}: {bad}", "fix it by hand (see `swarm bootstrap --host codex`)"))
    sw = data.get("sandbox_workspace_write") if isinstance(data.get("sandbox_workspace_write"), dict) else {}
    need = codex_config.required(cfg)[("sandbox_workspace_write", "writable_roots")]
    have_roots = sw.get("writable_roots") if isinstance(sw.get("writable_roots"), list) else []
    missing = [r for r in need if r not in have_roots]
    # declared in config.toml is not the same as usable: a root can be granted there and still be
    # absent on disk (an interrupted bootstrap, a deleted directory, ...), which fails the
    # sandbox's bind-mount the same way a root never granted at all does.
    not_on_disk = [r for r in need if r not in missing and not Path(r).is_dir()]
    mode = codex_config.effective_sandbox_mode(data)
    if mode == "read-only":        # the user's explicit choice: never changed by the swarm, a FAIL here
        out.append(Check("codex sandbox", False, 'sandbox_mode = "read-only"', codex_config.READ_ONLY_STEP))
    elif mode == "danger-full-access":
        out.append(Check("codex sandbox", True, "sandbox_mode = danger-full-access (no sandbox)"))
    else:
        ok = not missing and not not_on_disk
        out.append(Check("codex sandbox", ok, f"mode {mode or 'not set'}; "
                         f"missing roots: {', '.join(missing) or 'none'}"
                         + (f", granted but not on disk: {', '.join(not_on_disk)}" if not_on_disk else ""),
                         "~/.local/bin/swarm bootstrap --host codex"))
        if mode is None:
            out.append(Check("codex sandbox mode", None, "sandbox_mode not set: Codex picks it per session/project "
                             "(read-only in untrusted directories)",
                             'set sandbox_mode = "workspace-write" in ~/.codex/config.toml, or start swarm sessions '
                             "with `codex -s workspace-write`"))
    depth = (data.get("agents") if isinstance(data.get("agents"), dict) else {}).get("max_depth", 1)
    profiles = codex_config.profile_overrides(_codex_home(), data)
    out.append(Check("codex profiles", None if profiles else True,
                     f"override sandbox/agents: {', '.join(profiles)}" if profiles else "none override the sandbox",
                     codex_config.PROFILE_STEP.format(names=", ".join(profiles))))
    out.append(Check("codex depth", isinstance(depth, int) and not isinstance(depth, bool) and depth >= 2,
                     f"agents.max_depth = {depth if isinstance(depth, int) else 'not a number'}",
                     "~/.local/bin/swarm bootstrap --host codex"))
    if env.get("CODEX_THREAD_ID") or env.get("CODEX_SESSION_ID"):   # run from inside a Codex session
        failing = _sandbox_probe(codex_config.required(cfg)[("sandbox_workspace_write", "writable_roots")])
        out.append(Check("codex session", not failing, "this session can write the swarm's dirs" if not failing
                         else f"this session can't write: {', '.join(failing)}",
                         NEW_SESSION_STEP + " If it still fails, check the profile (-p) or -s the session was started with."))
    model = data.get("model")
    out.append(Check("orchestrator model", None,
                     f"codex: {model if isinstance(model, str) else 'host default'} (not set by the swarm)"))
    return out


def _sandbox_probe(roots: list[str]) -> list[str]:
    """The roots this process can't write (inside a Codex session: those the session's sandbox
    doesn't allow, or that are simply missing; a profile can drop one while keeping another). The
    board check covers the network. Never creates a root: a session whose sandbox was set up
    before the root existed has already failed to bind-mount it, so this reports it missing
    rather than quietly fixing it up after the fact (`swarm bootstrap --host codex` outside the
    session does the creating, before the sandbox is set up)."""
    failing = []
    for root in roots:
        p = Path(root)
        if not p.is_dir():
            failing.append(root)
            continue
        probe = p / f".doctor-probe-{os.getpid()}-{os.urandom(4).hex()}"
        try:   # a new file only (never through something planted at the name), then removed
            os.close(os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL | compat.O_NOFOLLOW | compat.O_CLOEXEC, 0o600))
            os.unlink(probe)
        except OSError:
            failing.append(root)
    return failing
