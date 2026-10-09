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

from swarm import channel as channels
from swarm import paths

_UNSET = object()   # "don't re-pin the marketplace": the pre-channel behaviour (tests, local sources)

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


def _claude_plugin_commit(plugins_dir: Path) -> str | None:
    """The git commit the installed swarm plugin was fetched at (installed_plugins.json's
    gitCommitSha), or None when it isn't recorded."""
    try:
        data = json.loads((plugins_dir / "installed_plugins.json").read_text())
        installs = (data.get("plugins") or {}).get(PLUGIN_SPEC) or []
        sha = installs[-1].get("gitCommitSha")
    except (OSError, ValueError, AttributeError, IndexError, TypeError):
        return None
    return sha if isinstance(sha, str) and sha else None


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


def _claude_marketplace_source(plugins_dir: Path) -> tuple[str | None, str | None] | None:
    """(url, ref) the swarm marketplace was added from, per Claude's known_marketplaces.json; None
    when it isn't registered or is a local directory/file (which can't be pinned to a ref)."""
    try:
        data = json.loads((plugins_dir / "known_marketplaces.json").read_text())
        src = data[MARKETPLACE_NAME]["source"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not isinstance(src, dict):
        return None
    kind, ref = src.get("source"), src.get("ref")
    ref = ref if isinstance(ref, str) and ref else None
    if kind == "github" and isinstance(src.get("repo"), str):
        return f"https://github.com/{src['repo']}.git", ref
    if kind == "git" and isinstance(src.get("url"), str):
        return src["url"], ref
    return None


def update_claude(bin_: str, ref=_UNSET, url: str | None = None, force: bool = False) -> dict:
    """ref: the ref to pin the marketplace at (a tag, or None for the default branch); _UNSET leaves
    the marketplace as it is. url: the git URL it is (re-)added from when the ref changes.
    force: reinstall the plugin even when its version is unchanged (a plugin install is a versioned
    cache, so `plugin update` refetches nothing when a version was re-cut at a new commit)."""
    plugins_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (paths.home() / ".claude")) / "plugins"
    old_version = _claude_plugin_version(plugins_dir)
    old_commit = _claude_plugin_commit(plugins_dir)
    old_root = _claude_plugin_root(plugins_dir)

    cur = _claude_marketplace_source(plugins_dir) if ref is not _UNSET else None
    repin = ref is not _UNSET and (cur is None or cur[1] != ref)
    if repin:
        # Claude takes the ref as `<url>#<ref>`. Removing the marketplace uninstalls its plugin, so
        # the plugin is installed again below.
        src = (cur[0] if cur else None) or url or DEFAULT_MARKETPLACE
        _run(bin_, ["plugin", "marketplace", "remove", MARKETPLACE_NAME])
        res = _run(bin_, ["plugin", "marketplace", "add", f"{src}#{ref}" if ref else src])
        if res.returncode != 0:
            raise UpdateError(f"[claude] plugin marketplace add {src}{'#' + ref if ref else ''} failed:\n{_out(res)}")
        verb = "install"
    else:
        res = _run(bin_, ["plugin", "marketplace", "update", MARKETPLACE_NAME])
        if res.returncode != 0:
            raise UpdateError(f"[claude] plugin marketplace update {MARKETPLACE_NAME} failed:\n{_out(res)}")
        verb = "update" if _claude_plugin_has_update_subcommand(bin_) else "install"
        if force:
            # --keep-data: only the plugin cache is replaced, never the plugin's data directory.
            res = _run(bin_, ["plugin", "uninstall", "--keep-data", PLUGIN_SPEC])
            if res.returncode != 0:
                raise UpdateError(f"[claude] plugin uninstall {PLUGIN_SPEC} failed:\n{_out(res)}")
            verb = "install"
    res2 = _run(bin_, ["plugin", verb, PLUGIN_SPEC])
    if res2.returncode != 0:
        raise UpdateError(f"[claude] plugin {verb} {PLUGIN_SPEC} failed:\n{_out(res2)}")

    new_version = _claude_plugin_version(plugins_dir)
    new_root = _claude_plugin_root(plugins_dir)
    return {"host": "claude", "old_version": old_version, "new_version": new_version,
            "changed": old_version != new_version, "old_root": old_root, "new_root": new_root,
            "verb": verb, "old_commit": old_commit, "new_commit": _claude_plugin_commit(plugins_dir)}


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


def _codex_plugin_commit(root: Path | None) -> str | None:
    """The commit of the installed Codex plugin copy at `root`, read from the copy itself (it is a git
    checkout only when Codex kept its .git). None when it can't be told: the marketplace's own
    state (config.toml last_revision) is never used, as it tracks the marketplace, not what is
    installed."""
    if root is None or not (root / ".git").exists():
        return None
    try:
        res = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True,
                             timeout=30, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    sha = res.stdout.strip()
    return sha if res.returncode == 0 and re.fullmatch(r"[0-9a-f]{40,64}", sha) else None


def update_codex(bin_: str, ref=_UNSET, url: str | None = None, force: bool = False) -> dict:
    """ref/url/force: as for update_claude (Codex takes the ref as `marketplace add <url> --ref <ref>`)."""
    old_version = _codex_plugin_version(bin_)
    old_root = newest_installed_plugin_root("codex", codex_bin=bin_)
    old_commit = _codex_plugin_commit(old_root)   # before any refresh, from the installed copy

    repin = ref is not _UNSET and (_codex_config_marketplace_field(MARKETPLACE_NAME, "ref") or None) != ref
    if repin:
        src = url or _codex_marketplace_source(bin_) or DEFAULT_MARKETPLACE
        _run(bin_, ["plugin", "marketplace", "remove", MARKETPLACE_NAME])
        res = _run(bin_, ["plugin", "marketplace", "add", src, *(["--ref", ref] if ref else [])])
        if res.returncode != 0:
            raise UpdateError(f"[codex] plugin marketplace add {src}{' --ref ' + ref if ref else ''} "
                              f"failed:\n{_out(res)}")
    else:
        res = _codex_marketplace_refresh(bin_)
    if not repin and res.returncode != 0:
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

    if force:
        _run(bin_, ["plugin", "remove", PLUGIN_SPEC])   # best effort: `plugin add` below installs it anyway
    res3 = _run(bin_, ["plugin", "add", PLUGIN_SPEC])
    if res3.returncode != 0:
        raise UpdateError(f"[codex] plugin add {PLUGIN_SPEC} failed:\n{_out(res3)}")

    new_version = _codex_plugin_version(bin_)
    new_root = newest_installed_plugin_root("codex", codex_bin=bin_)
    return {"host": "codex", "old_version": old_version, "new_version": new_version,
            "changed": old_version != new_version, "old_root": old_root, "new_root": new_root,
            "verb": "add", "old_commit": old_commit, "new_commit": _codex_plugin_commit(new_root)}


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

def _host_source(host: str, bin_: str) -> tuple[str | None, bool]:
    """(git url, pinnable): where this host's swarm marketplace comes from. A local path (a frozen
    tree, a checkout) is not pinnable: the channel does not apply to it."""
    if host == "claude":
        plugins_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (paths.home() / ".claude")) / "plugins"
        cur = _claude_marketplace_source(plugins_dir)
        if cur is not None:
            return cur[0], True
        try:
            data = json.loads((plugins_dir / "known_marketplaces.json").read_text())
            registered = isinstance(data, dict) and MARKETPLACE_NAME in data
        except (OSError, ValueError):
            registered = False
        return (None, False) if registered else (DEFAULT_MARKETPLACE, True)
    src = _codex_marketplace_source(bin_)
    if src is None:
        return DEFAULT_MARKETPLACE, True
    if os.path.isabs(os.path.expanduser(src)) or src.startswith((".", "~")) or re.match(r"^[A-Za-z]:[\\/]", src):
        return None, False
    if re.match(r"^[\w.-]+/[\w.-]+$", src):   # owner/repo shorthand
        return f"https://github.com/{src}.git", True
    return src, True


# --------------------------------------------------------------------------- installed commit record
#
# On the main channel an install is compared with the tip of main by commit, not by version (a
# version string is not bumped on every commit to main). Claude records the commit it fetched
# (installed_plugins.json gitCommitSha); a Codex copy only when it kept its .git. Where the host
# records none, the upgrade records the tip it installed from here, per host and install root.

COMMIT_RECORD = "installed-commits.json"
_SHA = re.compile(r"[0-9a-f]{40,64}")


def _read_commit_records() -> dict:
    try:
        from swarm import safefs
        with safefs.dir_fd(paths.host_dir(), create=False, strict_mode=0o700) as d:
            data = json.loads(safefs.read_text(d, COMMIT_RECORD) or "{}")
    except Exception:   # missing, unreadable or not this user's private dir: nothing recorded
        return {}
    return data if isinstance(data, dict) else {}


def recorded_commit(host: str, root: Path | None) -> str | None:
    """The commit this machine's last main-channel upgrade installed for `host` at `root`, if
    recorded (and `root` is still the install it was recorded for)."""
    rec = _read_commit_records().get(host)
    if not (isinstance(rec, dict) and root is not None and rec.get("root") == str(root)):
        return None
    sha = rec.get("sha")
    return sha if isinstance(sha, str) and _SHA.fullmatch(sha) else None


def record_commit(host: str, root: Path | None, sha: str | None) -> None:
    """Remember that `host`'s plugin at `root` was installed at `sha`. Never raises."""
    if root is None or not (isinstance(sha, str) and _SHA.fullmatch(sha)):
        return
    try:
        from swarm import safefs
        data = _read_commit_records()
        data[host] = {"root": str(root), "sha": sha}
        with safefs.dir_fd(paths.host_dir(), strict_mode=0o700) as d:
            safefs.write_atomic(d, COMMIT_RECORD, json.dumps(data, indent=1) + "\n")
    except Exception:
        pass


def _claude_plugins_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or (paths.home() / ".claude")) / "plugins"


def installed_commit(host: str, bin_: str) -> tuple[str | None, Path | None]:
    """(commit, root) of the installed plugin before an upgrade: the host's own record, else the
    one record_commit kept for that root, else (None, root)."""
    if host == "claude":
        root = _claude_plugin_root(_claude_plugins_dir())
        sha = _claude_plugin_commit(_claude_plugins_dir())
    else:
        root = newest_installed_plugin_root("codex", codex_bin=bin_)
        sha = _codex_plugin_commit(root)
    return sha or recorded_commit(host, root), root


def _same_commit(a: str | None, b: str | None) -> bool:
    return bool(a and b and (a.startswith(b) or b.startswith(a)))


def _short(sha: str | None) -> str:
    return sha[:7] if sha else "unknown"


def _commit_moved(r: dict | None) -> bool:
    return bool(r and r.get("old_commit") and r.get("new_commit") and r["old_commit"] != r["new_commit"])


def _behind_notes(hosts: list[str], results: dict[str, dict], tips: dict[str, str | None]) -> list[str]:
    """Lines to print instead of "swarm is up to date" on the main channel when the installed
    plugin may be older than the tip of main although its version matches (a version re-cut at a new
    commit is not refetched). Empty when every host is known to be at the tip, or this is not the
    main channel."""
    notes = []
    for host in hosts:
        if host not in tips:
            continue
        have, tip = results[host].get("new_commit"), tips[host]
        ver = results[host]["new_version"] or results[host]["old_version"] or "unknown"
        if not have:
            notes.append(f"[{host}] swarm {ver}: the installed commit can't be determined, so it can't "
                         f"be compared with the tip of main; run `swarm upgrade --force` to reinstall "
                         f"from the tip of main")
        elif not tip:
            notes.append(f"[{host}] swarm {ver} at {_short(have)}: the tip of main can't be read "
                         f"(git ls-remote failed), so it can't be compared")
        elif not (have.startswith(tip) or tip.startswith(have)):
            notes.append(f"[{host}] swarm {ver} is behind main: installed {_short(have)}, tip of main "
                         f"{_short(tip)}; run `swarm upgrade --force` to reinstall from the tip")
    return notes


def run_update(host_arg: str | None, force: bool, color: bool, config_path: Path | None = None,
               which=None, out=None, channel: str | None = None) -> int:
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

    want = channel or channels.read_channel(config_path) or channels.DEFAULT_CHANNEL
    results: dict[str, dict] = {}
    tips: dict[str, str | None] = {}   # host -> tip of main, on the main channel only
    fkw = {"force": True} if force else {}
    for host in hosts:
        bin_ = which(host)
        try:
            url, pinnable = _host_source(host, bin_)
            if not pinnable:
                print(f"[{host}] channel: the swarm marketplace is a local path: following it as is "
                      f"(--channel does not apply)", file=out)
                results[host] = update_claude(bin_, **fkw) if host == "claude" else update_codex(bin_, **fkw)
                continue
            chan, ref, warning = channels.resolve(want, url)
            if warning:
                print(f"swarm upgrade: [{host}] warning: {warning}", file=sys.stderr)
            print(f"[{host}] channel: {chan} ({ref or 'tip of main'})", file=out)
            have = None
            hkw = fkw
            if chan == "main" and url:
                tip = tips[host] = channels.tip_commit(url)
                have, _ = installed_commit(host, bin_)
                if tip and not force and not _same_commit(have, tip):
                    # same version string, other commit: a plain update would refetch nothing
                    print(f"[{host}] installed commit {_short(have)}, tip of main {_short(tip)}: "
                          f"reinstalling from the tip", file=out)
                    hkw = {"force": True}
            r = results[host] = (update_claude(bin_, ref, url, **hkw) if host == "claude"
                                 else update_codex(bin_, ref, url, **hkw))
            if chan == "main" and tips.get(host):
                r["old_commit"] = r.get("old_commit") or have
                if r.get("new_commit") is None:   # the host records none: ours, or the tip just installed
                    r["new_commit"] = tips[host] if hkw.get("force") else recorded_commit(host, r.get("new_root"))
                r["reinstalled"] = bool(hkw.get("force"))
                record_commit(host, r.get("new_root"), r.get("new_commit"))
                r["changed"] = bool(r["changed"] or _commit_moved(r) or (hkw.get("force") and not have))
        except UpdateError as exc:
            print(f"swarm update: {exc}", file=sys.stderr)
            return 1

    if channel:   # an explicit choice is remembered: a plain `swarm upgrade` keeps following it
        channels.write_channel(channel, config_path)

    steps = []
    for host in hosts:
        r = results[host]
        old = r["old_version"] or "none"
        new = r["new_version"] or "unknown"
        status = "changed" if r["changed"] else "ok"
        steps.append(bootstrap.Step(f"{host} plugin", status, f"{old} -> {new}"))
    print(bootstrap.format_steps(steps, color), file=out)

    for host in hosts:
        r = results[host]
        if force or r.get("reinstalled"):
            print(f"[{host}] commit: {_short(r.get('old_commit'))} -> {_short(r.get('new_commit'))}"
                  + (f" (tip of main {_short(tips[host])})" if tips.get(host) else ""), file=out)

    any_changed = any(r["changed"] for r in results.values())
    if not any_changed and not force:
        behind = _behind_notes(hosts, results, tips)
        if behind:
            for line in behind:
                print(line, file=out)
            return 0
        versions = {r["new_version"] or r["old_version"] or "unknown" for r in results.values()}
        v = versions.pop() if len(versions) == 1 else "/".join(sorted(versions))
        print(f"swarm is up to date ({v})", file=out)
        return 0

    swarm_bins: dict[str, Path] = {}
    for host in hosts:
        root = results[host]["new_root"] or newest_installed_plugin_root(host)
        launcher = "swarm.cmd" if paths.IS_WINDOWS else "swarm"
        if root is None or not os.access(root / "bin" / launcher, os.X_OK):
            print(f"swarm update: [{host}] can't find the installed swarm plugin's bin/swarm after "
                  f"updating; check '{which(host)} plugin list', then re-run swarm update",
                  file=sys.stderr)
            return 1
        swarm_bins[host] = root / "bin" / launcher

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

    if results.get("claude", {}).get("changed") or _commit_moved(results.get("claude")):
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
