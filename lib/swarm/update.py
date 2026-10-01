"""`swarm update` (spec: "one command that does the marketplace update and the plugin update for
whichever of Claude and Codex are installed, then bootstrap and doctor, with the same colours and
--force").

Host detection reuses shutil.which, the same primitive bootstrap.supervisor_checks already uses
to find the claude/codex binaries. The marketplace/plugin update itself runs through each host's
own CLI (never edits its config files directly). bootstrap/migrate/doctor are then run through the
*newly installed* plugin's own bin/swarm -- this process is running the old plugin's code, which
must not be the one that bootstraps the new one (locate_swarm_bin's reasoning in install.sh).
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from swarm import paths

MARKETPLACE_NAME = "swarm"
PLUGIN_SPEC = "swarm@swarm"
DEFAULT_MARKETPLACE = "https://github.com/fcarucci/Swarm.git"
HOST_NAMES = ("claude", "codex")
_TIMEOUT = 180


class UpdateError(Exception):
    """A host command failed, or no host CLI could be found; str(exc) is the message to print."""


def _run(bin_: str, args: list[str], timeout: int = _TIMEOUT) -> subprocess.CompletedProcess:
    """Runs bin_ args, or a synthetic failed result (returncode 127, the exception's text as
    stderr) when bin_ can't even be started (missing, not executable, times out): callers already
    treat any non-zero returncode as "this CLI call failed" via _out(), so a bad binary behaves
    the same as one that ran and printed an error, never an unhandled crash."""
    try:
        return subprocess.run([bin_, *args], capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess([bin_, *args], 127, "", str(exc))


def _out(res: subprocess.CompletedProcess) -> str:
    return (res.stdout + res.stderr).strip()


# --------------------------------------------------------------------------- host detection

def detect_hosts(host_arg: str | None, which=None) -> list[str]:
    """The host CLIs to update: --host claude|codex|both if given (an error if that one isn't on
    PATH), else every claude/codex CLI found on PATH."""
    which = which or shutil.which
    if host_arg in ("claude", "codex"):
        if not which(host_arg):
            raise UpdateError(f"--host {host_arg} given, but no '{host_arg}' binary found on PATH")
        return [host_arg]
    found = [h for h in HOST_NAMES if which(h)]
    if not found:
        raise UpdateError("no claude or codex CLI found on PATH; install one first "
                          "(see install.sh), or pass --host claude|codex|both")
    return found


# --------------------------------------------------------------------------- installed version / root

def _claude_plugin_root(plugins_dir: Path) -> Path | None:
    try:
        data = json.loads((plugins_dir / "installed_plugins.json").read_text())
    except (OSError, ValueError):
        return None
    for key, installs in (data.get("plugins") or {}).items():
        if key.split("@")[0] == "swarm" and installs:
            p = installs[-1].get("installPath")
            return Path(p) if p else None
    return None


def _claude_plugin_version(plugins_dir: Path) -> str | None:
    root = _claude_plugin_root(plugins_dir)
    if root is None or not root.exists():
        return None
    v = paths.plugin_version(root)
    return v if v != "0" else None


def _codex_plugin_version(bin_: str) -> str | None:
    res = _run(bin_, ["plugin", "list", "--json"])
    if res.returncode != 0:
        return None
    try:
        data = json.loads(res.stdout)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    installed = data.get("installed")
    if not isinstance(installed, list):
        return None
    for p in installed:
        if not isinstance(p, dict):
            continue
        if str(p.get("pluginId", "")).split("@")[0] == "swarm":
            v = p.get("version")
            if v:
                return str(v)
    return None


def newest_installed_plugin_root(host: str, claude_config_dir: Path | None = None,
                                  codex_home: Path | None = None, codex_bin: str = "codex") -> Path | None:
    """Port of install.sh's newest_installed_plugin: prefers what the host's plugin manager
    actually reports as installed (Claude: installed_plugins.json installPaths for swarm@swarm;
    Codex: the plugin cache directory whose manifest version matches what `codex plugin list
    --json` reports) over the cache glob's highest version number. Falls back to the cache glob's
    highest manifest version, most recently modified on a tie, only when nothing is reported --
    otherwise a stale higher-numbered cache directory left behind by an earlier install (e.g. an
    old .../swarm/1.0.0 after upgrading to 0.1.0) would outrank the fresh, actually-installed one
    forever. None when there is no candidate with an executable bin/swarm at all."""
    ccd = claude_config_dir or Path(os.environ.get("CLAUDE_CONFIG_DIR") or (paths.home() / ".claude"))
    chd = codex_home or Path(os.environ.get("CODEX_HOME") or (paths.home() / ".codex"))
    plugin = PLUGIN_SPEC.split("@")[0]
    reported: list[Path] = []
    cache_only: list[Path] = []

    def manifest_version_str(root: Path) -> str | None:
        for m in (".claude-plugin/plugin.json", ".codex-plugin/plugin.json"):
            try:
                v = json.loads((root / m).read_text())["version"]
                return str(v)
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return None

    if host == "claude":
        try:
            data = json.loads((ccd / "plugins" / "installed_plugins.json").read_text())
            for inst in (data.get("plugins") or {}).get(PLUGIN_SPEC) or []:
                if isinstance(inst, dict) and inst.get("installPath"):
                    reported.append(Path(inst["installPath"]))
        except (OSError, ValueError):
            pass
        cache_only = [Path(p) for p in glob.glob(str(ccd / "plugins" / "cache" / MARKETPLACE_NAME / plugin / "*"))]
    elif host == "codex":
        cache_only = [Path(p) for p in glob.glob(str(chd / "plugins" / "cache" / MARKETPLACE_NAME / plugin / "*"))]
        installed_v = _codex_plugin_version(codex_bin)
        if installed_v:
            reported = [c for c in cache_only if manifest_version_str(c) == installed_v]
            cache_only = [c for c in cache_only if c not in reported]

    def version(root: Path) -> tuple:
        v = manifest_version_str(root)
        if v is None:
            return (0,)
        return tuple(int(p) if p.isdigit() else 0 for p in v.split("."))

    def usable(cands: list[Path]) -> list[Path]:
        seen: set[Path] = set()
        ok: list[Path] = []
        for c in cands:
            if c in seen:
                continue
            seen.add(c)
            if os.access(c / "bin" / "swarm", os.X_OK):
                ok.append(c)
        return ok

    ok = usable(reported) or usable(cache_only)
    if not ok:
        return None
    return max(ok, key=lambda r: (version(r), r.stat().st_mtime))


# --------------------------------------------------------------------------- per-host update

def _claude_plugin_has_update_subcommand(bin_: str) -> bool:
    res = _run(bin_, ["plugin", "--help"])
    text = res.stdout + res.stderr
    return bool(re.search(r"(?m)^\s*update\b", text))


def update_claude(bin_: str) -> dict:
    plugins_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (paths.home() / ".claude")) / "plugins"
    old_version = _claude_plugin_version(plugins_dir)
    old_root = _claude_plugin_root(plugins_dir)

    res = _run(bin_, ["plugin", "marketplace", "update", MARKETPLACE_NAME])
    if res.returncode != 0:
        raise UpdateError(f"[claude] plugin marketplace update {MARKETPLACE_NAME} failed:\n{_out(res)}")

    verb = "update" if _claude_plugin_has_update_subcommand(bin_) else "install"
    res2 = _run(bin_, ["plugin", verb, PLUGIN_SPEC])
    if res2.returncode != 0:
        raise UpdateError(f"[claude] plugin {verb} {PLUGIN_SPEC} failed:\n{_out(res2)}")

    new_version = _claude_plugin_version(plugins_dir)
    new_root = _claude_plugin_root(plugins_dir)
    return {"host": "claude", "old_version": old_version, "new_version": new_version,
            "changed": old_version != new_version, "old_root": old_root, "new_root": new_root,
            "verb": verb}


def _codex_config_marketplace_field(name: str, field: str) -> str | None:
    """`field` of `[marketplaces.<name>]` in $CODEX_HOME/config.toml, where codex records where a
    marketplace was added from (keys: source_type, source, ref, last_updated, ...)."""
    try:
        import tomllib
    except ImportError:  # Python < 3.11
        return None
    cfg = Path(os.environ.get("CODEX_HOME") or (paths.home() / ".codex")) / "config.toml"
    try:
        data = tomllib.loads(cfg.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    mp = data.get("marketplaces")
    entry = mp.get(name) if isinstance(mp, dict) else None
    val = entry.get(field) if isinstance(entry, dict) else None
    return val if isinstance(val, str) and val else None


def _codex_marketplace_entry(bin_: str):
    """(listed, entry): whether `codex plugin marketplace list --json` could be read, and the swarm
    entry in it (None when swarm isn't registered)."""
    res = _run(bin_, ["plugin", "marketplace", "list", "--json"])
    if res.returncode != 0:
        return False, None
    try:
        data = json.loads(res.stdout)
    except ValueError:
        return False, None
    entries = None
    if isinstance(data, list):
        entries = data
    elif isinstance(data, dict):
        for key in ("marketplaces", "installed"):
            if isinstance(data.get(key), list):
                entries = data[key]
                break
    for e in entries or []:
        if isinstance(e, dict) and e.get("name") == MARKETPLACE_NAME:
            return True, e
    return True, None


def _codex_marketplace_source(bin_: str) -> str | None:
    """The source (URL or path) the swarm marketplace is currently configured with, so a failed
    marketplace refresh can re-add the *same* source instead of guessing DEFAULT_MARKETPLACE --
    which would silently switch a user who configured something else (a local frozen tree, a
    different remote) to a different upstream. Read from `codex plugin marketplace list --json`
    (`{"marketplaces": [{"name", "root", "marketplaceSource": {"sourceType", "source"}}]}`), then
    from config.toml. The human table (`MARKETPLACE  ROOT`) is not used: ROOT is the local
    snapshot directory, not the source. None if nothing names it: callers must not guess in that
    case, only stop with the original error."""
    _, e = _codex_marketplace_entry(bin_)
    if e is not None:
        nested = e.get("marketplaceSource")
        cands = [nested.get("source")] if isinstance(nested, dict) else []
        cands += [e.get(k) for k in ("source", "url", "path", "repo")]
        for v in cands:
            if isinstance(v, str) and v:
                return v
    return _codex_config_marketplace_field(MARKETPLACE_NAME, "source")


def _dead_local_source(source: str) -> bool:
    """A source that is a local path which no longer exists (e.g. a codex staging dir that was
    cleaned up): it can never be re-added."""
    path = os.path.expanduser(source)
    return os.path.isabs(path) and not os.path.exists(path)


def _codex_marketplace_refresh(bin_: str) -> subprocess.CompletedProcess:
    """Refresh the swarm marketplace: `upgrade` on current codex (older CLIs call it `update`; same
    optional marketplace-name argument, openai/codex codex-rs/cli/src/marketplace_cmd.rs), or
    `update` when the CLI reports `upgrade` as an unrecognized subcommand (older codex)."""
    res = _run(bin_, ["plugin", "marketplace", "upgrade", MARKETPLACE_NAME])
    if res.returncode != 0 and re.search(r"unrecogni[sz]ed subcommand|unknown subcommand", _out(res), re.I):
        return _run(bin_, ["plugin", "marketplace", "update", MARKETPLACE_NAME])
    return res


def update_codex(bin_: str) -> dict:
    old_version = _codex_plugin_version(bin_)
    old_root = newest_installed_plugin_root("codex", codex_bin=bin_)

    res = _codex_marketplace_refresh(bin_)
    if res.returncode != 0:
        source = _codex_marketplace_source(bin_)
        listed, entry = _codex_marketplace_entry(bin_)
        registered = entry is not None or source is not None
        if source is None and listed and not registered:
            # The marketplace isn't registered at all (an earlier failed remove+add dropped it): there
            # is no configured source to keep, so register the public one.
            source, need_remove = DEFAULT_MARKETPLACE, False
        elif source is None:
            raise UpdateError(f"[codex] plugin marketplace refresh {MARKETPLACE_NAME} failed, and "
                              f"its configured source couldn't be read to safely re-add it (won't "
                              f"guess and switch to a different marketplace source):\n{_out(res)}")
        elif _dead_local_source(source):
            # The configured source is a local path that is gone: re-adding it can only fail (and
            # `remove` first would leave no marketplace), so use the public source instead.
            source, need_remove = DEFAULT_MARKETPLACE, True
        else:
            need_remove = True
        if need_remove:
            _run(bin_, ["plugin", "marketplace", "remove", MARKETPLACE_NAME])
        ref = None if source == DEFAULT_MARKETPLACE else _codex_config_marketplace_field(MARKETPLACE_NAME, "ref")
        res2 = _run(bin_, ["plugin", "marketplace", "add", source, *(["--ref", ref] if ref else [])])
        if res2.returncode != 0:
            raise UpdateError(f"[codex] plugin marketplace refresh {MARKETPLACE_NAME} failed, and "
                              f"remove+add of its configured source {source!r} also failed:\n"
                              f"{_out(res)}\n{_out(res2)}")

    res3 = _run(bin_, ["plugin", "add", PLUGIN_SPEC])
    if res3.returncode != 0:
        raise UpdateError(f"[codex] plugin add {PLUGIN_SPEC} failed:\n{_out(res3)}")

    new_version = _codex_plugin_version(bin_)
    new_root = newest_installed_plugin_root("codex", codex_bin=bin_)
    return {"host": "codex", "old_version": old_version, "new_version": new_version,
            "changed": old_version != new_version, "old_root": old_root, "new_root": new_root,
            "verb": "add"}


# --------------------------------------------------------------------------- hooks-changed detection

def _hash_file(p: Path | None) -> str | None:
    if p is None:
        return None
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()
    except OSError:
        return None


def codex_hooks_changed(old_root: Path | None, new_root: Path | None) -> bool:
    """Whether hooks/codex-hooks.json differs between the old and new plugin roots: only then is
    Codex's /hooks re-trust step worth printing (a plugin update that didn't touch hooks needs
    only a new session, which the general reminder already covers)."""
    if old_root is None or new_root is None:
        return False
    rel = Path("hooks/codex-hooks.json")
    return _hash_file(old_root / rel) != _hash_file(new_root / rel)


# --------------------------------------------------------------------------- delegate to the new plugin

def _child(sw: Path, args: list[str], color: bool, capture: bool = False):
    cmd = [str(sw), *args]
    if not color:
        cmd.append("--no-color")
    if capture:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)
        return res.returncode, res.stdout, res.stderr
    res = subprocess.run(cmd, timeout=300, stdin=subprocess.DEVNULL)
    return res.returncode, None, None


def _warn_forced_migrate(sw: Path, config_path: Path | None) -> None:
    """Mirrors install.sh's warn_forced_migrate: before forcing, list each job name the marker
    dir holds and whether it is still open on the board (read-only)."""
    from swarm import bootstrap, cli
    try:
        cfg = cli.load_config(config_path) if config_path else cli.load_config()
    except Exception:
        return
    try:
        jobs = bootstrap.active_jobs(bootstrap.marker_dir_of(cfg))
    except bootstrap.UnsafeMarkerDir:
        return
    if not jobs:
        return
    print(f"swarm upgrade: migrating anyway; swarm job marker(s) on this machine: {', '.join(jobs)}")
    rc, out, _ = _child(sw, ["status", "--all"], False, capture=True)
    board_status: dict[str, str] = {}
    if rc == 0:
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] not in ("JOB",):
                board_status.setdefault(parts[0], parts[1])
    for j in jobs:
        st = board_status.get(j)
        if st is None:
            print(f"    {j}: not on the board (stale marker; safe to override)")
        elif st in ("completed", "cancelled", "failed"):
            print(f"    {j}: closed on the board (status: {st})")
        else:
            print(f"    {j}: still {st} on the board -- forcing migrate anyway runs alongside a "
                  f"job that may still be using the old hooks. Double check before continuing.")


def _run_migrate(sw: Path, force: bool, color: bool, config_path: Path | None) -> int:
    if force:
        _warn_forced_migrate(sw, config_path)
    args = ["migrate"] + (["--force"] if force else [])
    rc, _, _ = _child(sw, args, color)
    return rc


# --------------------------------------------------------------------------- main entry point

def run_update(host_arg: str | None, force: bool, color: bool, config_path: Path | None = None,
               which=None, out=None) -> int:
    """Runs `swarm update`; returns the process exit code. Prints to `out` (default sys.stdout)
    and errors to sys.stderr. Never touches sudo/root, and never any user's home but this one's
    (every path here comes from $HOME / $CLAUDE_CONFIG_DIR / $CODEX_HOME, or a CLI's own output)."""
    # Everything this process imports after the host CLIs run must be loaded first: they replace the
    # plugin folder we run from, and under `python -m swarm.cli` this CLI is __main__, so a later
    # `from swarm.cli import ...` would read a deleted file (ModuleNotFoundError).
    import importlib
    for mod in ("swarm.cli", "swarm.bootstrap", "swarm.safefs"):   # safefs: _warn_forced_migrate -> active_jobs
        importlib.import_module(mod)
    from swarm import bootstrap
    out = out or sys.stdout
    which = which or shutil.which

    try:
        hosts = detect_hosts(host_arg, which=which)
    except UpdateError as exc:
        print(f"swarm update: {exc}", file=sys.stderr)
        return 1
    print(f"hosts: {' '.join(hosts)}", file=out)

    results: dict[str, dict] = {}
    for host in hosts:
        bin_ = which(host)
        try:
            results[host] = update_claude(bin_) if host == "claude" else update_codex(bin_)
        except UpdateError as exc:
            print(f"swarm update: {exc}", file=sys.stderr)
            return 1

    steps = []
    for host in hosts:
        r = results[host]
        old = r["old_version"] or "none"
        new = r["new_version"] or "unknown"
        status = "changed" if r["changed"] else "ok"
        steps.append(bootstrap.Step(f"{host} plugin", status, f"{old} -> {new}"))
    print(bootstrap.format_steps(steps, color), file=out)

    any_changed = any(r["changed"] for r in results.values())
    if not any_changed and not force:
        versions = {r["new_version"] or r["old_version"] or "unknown" for r in results.values()}
        v = versions.pop() if len(versions) == 1 else "/".join(sorted(versions))
        print(f"swarm is up to date ({v})", file=out)
        return 0

    swarm_bins: dict[str, Path] = {}
    for host in hosts:
        root = results[host]["new_root"] or newest_installed_plugin_root(host)
        if root is None or not os.access(root / "bin" / "swarm", os.X_OK):
            print(f"swarm update: [{host}] can't find the installed swarm plugin's bin/swarm after "
                  f"updating; check '{which(host)} plugin list', then re-run swarm update",
                  file=sys.stderr)
            return 1
        swarm_bins[host] = root / "bin" / "swarm"

    for host in hosts:
        rc, _, _ = _child(swarm_bins[host], ["bootstrap", "--host", host], color)
        if rc != 0:
            print(f"swarm update: [{host}] swarm bootstrap failed (see output above)", file=sys.stderr)
            return 1

    migrate_host = hosts[0]
    # Always forced: an active job only warns, it never blocks the upgrade.
    mrc = _run_migrate(swarm_bins[migrate_host], True, color, config_path)
    if mrc != 0:
        print("swarm update: swarm migrate failed (see output above)", file=sys.stderr)
        return 1

    doctor_failed = False
    for host in hosts:
        rc, _, _ = _child(swarm_bins[host], ["doctor", "--host", host], color)
        if rc != 0:
            doctor_failed = True

    if results.get("claude", {}).get("changed"):
        print("Restart your Claude sessions to pick up the new plugin.", file=out)
    codex_r = results.get("codex")
    if codex_r and codex_r["changed"] and codex_hooks_changed(codex_r["old_root"], codex_r["new_root"]):
        print("Codex: hooks changed -- start a new Codex session and run /hooks to re-trust them.",
              file=out)

    if doctor_failed:
        print("swarm update: swarm doctor reported at least one FAIL above; fix it, then re-run "
              "swarm update (idempotent)", file=sys.stderr)
        return 1
    return 0
