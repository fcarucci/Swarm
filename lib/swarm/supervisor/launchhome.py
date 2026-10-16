"""A drive launch's file view (Task 20; spec 4.0: the shell guard is a guardrail, this is the boundary). Every
drive launch (except a configured trusted Codex wrapper) goes through `wrap`, which runs it in swarm.launchjail with an
ALLOWLIST of the person's files:

- the launch's own directory, <supervisor private dir>/launch-home/<job>/<launch> (0700, privfs), holds:
  home/ (the launch's $HOME, writable scratch, with a copy of ~/.claude.json), tmp/ (its /tmp), claude/ and
  codex/ (its ~/.claude and ~/.codex: its own transcripts, todos and sessions, never another session's),
  .ssh/ (known_hosts and a config with no identity), gitconfig (an identity, no credential helper),
  broker/ (its only way to the board: swarm.supervisor.launchbroker) and bin/ (the `swarm` it runs: the
  broker client);
- bound back read-only: the harness's settings, plugins, skills, hooks and instructions (~/.claude/...), the
  Codex config and hooks, the swarm venv, the tools on PATH below the home, [drive] launch_ro_paths;
- bound back writable: the launch's work tree and its git dir (hooks and config read-only), the harness's
  login file (a token refresh writes it: the residual of spec 4.0, not a repository credential),
  [drive] launch_rw_paths;
- everything else under the person's home is not there (~/.config, ~/.local/share/pki, ~/.pg0, the
  supervisor's private dir, shell rc files, systemd units, ~/.ssh keys, gh, tokens, sockets), and paths a
  job pins outside the home are covered.

Fetch for launches: GitHub remotes are read over anonymous HTTPS. A private repository needs a READ-ONLY deploy
key per destination outside the home's hidden parts, named in [drive] launch_ssh_key (bound read-only).

Turning isolation off is a person's act: `swarm supervise launch-isolation off` (a person at a terminal,
person.require) writes a flag in the supervisor's private dir, which no launch sees; [drive] launch_isolation
in config.toml cannot turn it off."""
from __future__ import annotations

import glob
import json
import os
import re
import sys
import time
from pathlib import Path

from swarm import compat, paths

LAUNCH_HOME = "launch-home"
OFF_FLAG = "launch-isolation.off"
_SLUG = re.compile(r"[^A-Za-z0-9._-]+")
# ~/.claude entries a launch reads (read-only) and writes (the login file); everything else is the launch's own.
CLAUDE_RO = ("settings.json", "settings.local.json", "CLAUDE.md", "RTK.md", "plugins", "skills", "agents",
             "commands", "hooks", "output-styles", "keybindings.json", "policy-limits.json",
             "remote-settings.json")
CLAUDE_RW = (".credentials.json", ".credentials.lock")
CODEX_RO = ("config.toml", "hooks.json", "AGENTS.md", "rules", "prompts")
CODEX_RW = ("auth.json",)
DEFAULT_RO = (".rustup", ".cargo")
# The credential locations of the other platforms (Task 21 ports the jail itself; the allowlist above
# already excludes them on Linux). The macOS Keychain (gh's token by default) and the Windows Credential
# Manager / ssh-agent service are services, not files: a platform jail must deny those, not paths.
MACOS_COVER = ("Library/Application Support/gh", "Library/Application Support/GitHub CLI", "Library/Keychains")
WINDOWS_COVER = (".ssh", "AppData/Roaming/GitHub CLI", ".git-credentials", ".gitconfig",
                 "AppData/Local/GitCredentialManager", "AppData/Roaming/GitCredentialManager", "_netrc")


class LaunchIsolationError(RuntimeError):
    """A drive launch that cannot be launched in its jail: never started."""


def _drive(cfg: dict) -> dict:
    d = (cfg or {}).get("drive")
    if not isinstance(d, dict):
        return {}
    # Accept legacy drive keys; an explicitly set launch value always wins.
    out = dict(d)
    prefix = "se" "at_"
    for key, value in d.items():
        if key.startswith(prefix):
            out.setdefault("launch_" + key[len(prefix):], value)
    return out


def _list(cfg: dict, key: str) -> list[str]:
    v = _drive(cfg).get(key) or []
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise LaunchIsolationError(f"[drive] {key} must be a list of paths")
    return [os.path.normpath(os.path.expanduser(x)) for x in v]


# --- isolation on/off: a person's act ---------------------------------------------------------------

def off_granted() -> dict | None:
    """The person's off flag (who, when) in the supervisor's private dir, or None."""
    from swarm.supervisor import privfs
    try:
        with privfs.dir_fd(create=False) as d:
            raw = privfs.read(d, OFF_FLAG)
    except FileNotFoundError:
        return None
    except Exception:
        return None          # unreadable: isolation stays on (fail closed)
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except ValueError:
        return None


def jail_platform(platform: str | None = None) -> bool:
    """Whether launches on `platform` (sys.platform) get the filesystem jail (Linux); elsewhere they are best-effort."""
    return (platform or sys.platform).startswith("linux")


def strict(cfg: dict) -> bool:
    """[drive] launch_isolation = "strict": the person's opt-in to fail closed. A launch is never started without the
    full jail (macOS, Windows and a Linux host without unprivileged user namespaces refuse drive launches)."""
    value = str(_drive(cfg).get("launch_isolation", "required")).strip().lower()
    if value not in ("required", "strict", "off"):
        raise LaunchIsolationError(f"[drive] launch_isolation must be \"required\" or \"strict\", not {value!r}")
    return value == "strict"


def mode(cfg: dict, platform: str | None = None) -> str:
    """"required" (Linux: the jail where it can be built) or "best-effort" (macOS, Windows: no filesystem jail;
    the launch gets the allowlisted environment, no credential helper, no agent, a scratch HOME and the broker),
    unless a person turned isolation off on this host (`swarm supervise launch-isolation off`): "off". [drive]
    launch_isolation in config.toml cannot turn isolation off ("off" there is ignored); "strict" makes a host that
    cannot jail refuse launches instead of falling back to best-effort (launch)."""
    strict(cfg)   # validates the value
    if off_granted() is not None:
        return "off"
    return "required" if jail_platform(platform) else "best-effort"


def set_off(off: bool, *, env=None, confirm=None, ancestor=None) -> str:
    """Turn launch isolation off (or back on) for this host: a person's act (person.require: a person at a
    terminal outside any agent host, who types y). Returns who confirmed."""
    from swarm import person
    from swarm.board.autoinit import store_key
    from swarm.supervisor import privfs
    action = "turn drive launch isolation OFF (launches can read your credentials) on" if off else \
        "turn drive launch isolation back on for"
    who = person.require(action, "this host", board_key=store_key({}), who=None,
                         env=os.environ if env is None else env, confirm=confirm, ancestor=ancestor)
    with privfs.dir_fd() as d:
        if off:
            privfs.write_atomic(d, OFF_FLAG, json.dumps({"by": who, "at": time.time()}), durable=True)
        else:
            try:
                privfs.unlink(d, OFF_FLAG)
            except FileNotFoundError:
                pass
    return who


def cmd_launch_isolation(cfg: dict, args) -> int:
    from swarm import person
    want = getattr(args, "state", None)
    if want not in ("on", "off"):
        flag = off_granted()
        print("launch isolation: " + ("OFF (by a person, at " + time.strftime("%Y-%m-%d %H:%M", time.localtime(
            flag.get("at", 0))) + ")" if flag else "on"))
        return 0
    try:
        set_off(want == "off")
    except person.PersonRefused as exc:
        print(f"swarm supervise launch-isolation: {exc}", file=sys.stderr)
        return 1
    print(f"launch isolation {want}")
    return 0


# --- the launch's directory ---------------------------------------------------------------------------

def _slug(name: str) -> str:
    import hashlib
    s = _SLUG.sub("_", str(name)).strip("._") or "x"
    return f"{s[:60]}-{hashlib.sha256(str(name).encode()).hexdigest()[:8]}"


def migrate_home() -> None:
    """Move the legacy private home tree once, before a launch uses it."""
    from swarm import compat
    from swarm.supervisor import privfs
    from swarm.supervisor.settings import private_dir
    old = "se" "at-home"
    base = private_dir()
    if not os.path.lexists(base / old):
        return
    with privfs.dir_fd(create=False) as d:
        with privfs.locked(d, "launch-home-migration.lock"):
            if not os.path.lexists(base / old):
                return
            if (base / old).is_symlink() or not (base / old).is_dir():
                raise LaunchIsolationError("legacy launch home must be a real directory")
            if os.path.lexists(base / LAUNCH_HOME):
                raise LaunchIsolationError("both legacy and current launch homes exist; refusing to overwrite")
            compat.rename(old, LAUNCH_HOME, src_dir_fd=d, dst_dir_fd=d)


def home_dir(job: str, launch: str) -> Path:
    from swarm.supervisor.settings import private_dir
    migrate_home()
    return private_dir() / LAUNCH_HOME / _slug(job) / _slug(launch)


def remove_job_homes(job: str) -> int:
    """Delete every launch home of `job` (the closed job's garbage: 2026-10-15) and return the bytes freed.
    Only the job's own <private dir>/launch-home/<slug(job)> is removed, never through a symlink: a link at
    that path is unlinked, not followed."""
    import shutil
    from swarm.supervisor.settings import private_dir
    migrate_home()
    root = private_dir() / LAUNCH_HOME
    target = root / _slug(job)
    if target.parent != root:
        return 0
    if target.is_symlink():
        target.unlink()
        return 0
    if not target.is_dir():
        return 0
    freed = 0
    for dirpath, _dirs, files in os.walk(target, followlinks=False):
        for f in files:
            try:
                freed += os.lstat(os.path.join(dirpath, f)).st_size
            except OSError:
                pass
    shutil.rmtree(target, ignore_errors=True)
    return freed


def _git_identity(cfg: dict) -> tuple[str | None, str | None]:
    """[drive] launch_git_name / launch_git_email, else the person's global git identity."""
    d = _drive(cfg)
    name, email = d.get("launch_git_name"), d.get("launch_git_email")
    if name and email:
        return str(name), str(email)
    import subprocess

    def get(key):
        try:
            r = subprocess.run(["git", "config", "--global", "--get", key], capture_output=True, text=True,
                               timeout=10, env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1"})
        except (OSError, subprocess.SubprocessError):
            return None
        return (r.stdout.strip() or None) if r.returncode == 0 else None
    return str(name) if name else get("user.name"), str(email) if email else get("user.email")


def _clean(value: str) -> str:
    return "".join(c for c in value if c.isprintable() and c not in '"\\\n')


def gitconfig_text(cfg: dict) -> str:
    name, email = _git_identity(cfg)
    lines = []
    if name or email:
        lines.append("[user]")
        if name:
            lines.append(f'\tname = "{_clean(name)}"')
        if email:
            lines.append(f'\temail = "{_clean(email)}"')
    lines += ["[credential]", "\thelper =", "[core]", "\taskPass =",
              '[url "https://github.com/"]', "\tinsteadOf = git@github.com:",
              "\tinsteadOf = ssh://git@github.com/"]
    return "\n".join(lines) + "\n"


def ssh_config_text(cfg: dict) -> str:
    key = _drive(cfg).get("launch_ssh_key")
    lines = ["# written by the swarm supervisor: a drive launch's ssh names no push identity",
             "Host *", "\tIdentityAgent none", "\tIdentitiesOnly yes", "\tBatchMode yes",
             "\tForwardAgent no", "\tControlMaster no", "\tControlPath none"]
    if key:
        if not os.path.isabs(str(key)):
            raise LaunchIsolationError("[drive] launch_ssh_key must be an absolute path (a read-only deploy key)")
        lines.append(f'\tIdentityFile "{_clean(str(key))}"')
    else:
        lines.append("\tIdentityFile /dev/null")
    return "\n".join(lines) + "\n"


CLIENT_SH = """#!/bin/sh
# the swarm command of a jailed drive launch: everything goes to the supervisor through the launch's broker
exec python3 -I "$(dirname "$0")/launchbroker_client.py" cli "$@"
"""


def client_scripts(platform: str | None = None, python: str | None = None) -> dict:
    """The launch's `swarm` command(s): in the Linux jail the system python3; on a best-effort host the
    supervisor's own python, by absolute path (a Windows launch gets swarm.cmd too)."""
    if jail_platform(platform):
        return {"swarm": CLIENT_SH}
    py = python or sys.executable
    out = {"swarm": "#!/bin/sh\n# the swarm command of a drive launch: everything goes to the supervisor through "
                    f"the launch's broker\nexec \"{py}\" -I \"$(dirname \"$0\")/launchbroker_client.py\" cli \"$@\"\n"}
    if (platform or sys.platform).startswith("win"):
        out["swarm.cmd"] = f'@echo off\r\n"{py}" -I "%~dp0launchbroker_client.py" cli %*\r\nexit /b %ERRORLEVEL%\r\n'
    return out


def _windows_readonly(path: Path, on: bool) -> None:
    """On Windows, set or clear a file's read-only attribute (POSIX modes are not enforced there); elsewhere
    nothing (the file mode does it). Best effort: a missing file is fine."""
    if os.name != "nt":
        return
    import stat
    try:
        os.chmod(path, stat.S_IREAD if on else stat.S_IREAD | stat.S_IWRITE)
    except OSError:
        pass


def _read_person(path: Path) -> bytes | None:
    """A regular file of the person's (or a launch's .git file), read without following a link: O_NOFOLLOW where the
    platform has it; on Windows (no O_NOFOLLOW) an explicit link check first, and the open refused if that fails."""
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        if not nofollow and os.path.islink(path):
            return None
        fd = os.open(path, os.O_RDONLY | nofollow | getattr(os, "O_BINARY", 0))
    except OSError:
        return None
    try:
        import stat
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        return os.read(fd, 16 * 1024 * 1024)
    finally:
        os.close(fd)


LAUNCH_SWARM_COMMANDS = ("post", "read", "done", "review", "verdict", "goal-ready", "land", "ask", "answer", "run",
                       "who", "status",
                       "remember", "recall", "learn")   # the memory life cycle: every launch stores or declares; a learner learns
LAUNCH_ALLOW = tuple(f"Bash(swarm {c}:*)" for c in LAUNCH_SWARM_COMMANDS) + ("Bash(make test:*)",)
LAUNCH_SETTINGS = "claude-settings.json"     # in the launch's directory: ~/.claude/settings.json for the launch


def launch_settings(person: bytes | None) -> str:
    """The launch's Claude user settings: the person's ~/.claude/settings.json (hooks, plugins, everything kept) with
    `permissions.allow` rules for exactly the documented launch commands added (LAUNCH_ALLOW), so the harness's
    auto-mode classifier does not deny a launch's own `swarm done`. Nothing broader: the broker, the hook guard and
    the jail still decide what runs. Unreadable or non-object settings: just the rules."""
    try:
        data = json.loads(person) if person else {}
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    perms = data.get("permissions") if isinstance(data.get("permissions"), dict) else {}
    allow = [r for r in (perms.get("allow") or []) if isinstance(r, str)] if isinstance(perms.get("allow"), list) else []
    perms = {**perms, "allow": allow + [r for r in LAUNCH_ALLOW if r not in allow]}
    return json.dumps({**data, "permissions": perms}, indent=2)


def prepare(cfg: dict, job: str, launch: str) -> Path:
    """Create or refresh the launch's directory (privfs: no link followed, this user's, 0700); its path."""
    from swarm import safefs
    from swarm.supervisor import privfs
    from swarm.supervisor.settings import PrivateDirError
    migrate_home()
    home = paths.home()
    known = _read_person(home / ".ssh" / "known_hosts") or b""
    claude_json = _read_person(home / ".claude.json")
    person_settings = _read_person(home / ".claude" / "settings.json")
    client = (Path(__file__).resolve().parent.parent / "launchbroker_client.py").read_bytes()
    proto = (Path(__file__).resolve().parent.parent / "launchproto.py").read_bytes()
    fds = []
    try:
        fds.append(privfs.open_dir(LAUNCH_HOME))
        for name in (_slug(job), _slug(launch)):
            fds.append(safefs.open_sub(fds[-1], name, strict_mode=0o700))
        h = fds[-1]
        for sub in ("home", "tmp", "claude", "codex", ".ssh", "broker", "bin", "gh"):
            os.close(safefs.open_sub(h, sub, strict_mode=0o700))
        privfs.write_atomic(h, "gitconfig", gitconfig_text(cfg))
        settings_path = home_dir(job, launch) / LAUNCH_SETTINGS
        _windows_readonly(settings_path, False)    # Windows cannot replace a read-only file: clear it first
        privfs.write_atomic(h, LAUNCH_SETTINGS, launch_settings(person_settings), 0o400)   # read-only for the launch
        _windows_readonly(settings_path, True)     # Windows: the read-only attribute (POSIX: mode 0400 above)

        def into(sub, name, data, mode=0o600):
            d = safefs.open_sub(h, sub, strict_mode=0o700)
            try:
                privfs.write_atomic(d, name, data, mode)
            finally:
                os.close(d)
        into(".ssh", "known_hosts", known)
        into(".ssh", "config", ssh_config_text(cfg))
        for script, text in client_scripts().items():
            into("bin", script, text, 0o700)
        into("bin", "launchbroker_client.py", client, 0o600)
        into("bin", "launchproto.py", proto, 0o600)
        if claude_json is not None:
            into("home", ".claude.json", claude_json)
        _seed_standing_rules(h, home / ".cache" / "coder-box" / "standing-rules.json")
    except (safefs.UnsafePathError, PrivateDirError, OSError) as exc:
        raise LaunchIsolationError(f"the launch's directory cannot be prepared safely: {exc}") from exc
    finally:
        for fd in fds:
            os.close(fd)
    return home_dir(job, launch)


def _seed_standing_rules(h: int, source: Path) -> None:
    """Carry the person's cached rules into the scratch home; keep a launch's newer cache."""
    from swarm import safefs
    from swarm.supervisor import privfs
    try:
        modified = source.stat().st_mtime
    except OSError:
        return
    data = _read_person(source)
    if not data:
        return
    fds = []
    try:
        fds.append(safefs.open_sub(h, "home", strict_mode=0o700))
        for name in (".cache", "coder-box"):
            # Only these driver-owned seed dirs may have their modes repaired. Never follow a link
            # or chmod another user's directory; tighten the opened descriptor before walking on.
            try:
                compat.mkdir(name, 0o700, dir_fd=fds[-1])
            except FileExistsError:
                pass
            fds.append(compat.open(name, safefs.DIR_FLAGS, dir_fd=fds[-1]))
            if compat.HAS_MODES and os.fstat(fds[-1]).st_uid != compat.uid():
                raise safefs.UnsafePathError(f"{name} belongs to another user: refusing to use it")
            compat.fchmod(fds[-1], 0o700)
        d, name = fds[-1], "standing-rules.json"
        if not privfs.read(d, name) or modified > (privfs.mtime(d, name) or 0):
            privfs.write_atomic(d, name, data, 0o600)
        else:
            fd = safefs.open_existing(d, name, os.O_RDONLY)
            try:
                compat.fchmod(fd, 0o600)
            finally:
                os.close(fd)
    finally:
        for fd in fds:
            os.close(fd)


# --- what the launch sees -----------------------------------------------------------------------------

def _pin_paths(owner: dict) -> list[str]:
    """The paths a drive job's pinned credentials name: ssh keys (and their .pub), agent sockets, gh dirs."""
    out = []
    for pin in (owner.get("credentials") or {}).values():
        kind, _, value = str(pin).partition(":")
        if kind == "ssh-key" and os.path.isabs(value):
            out += [value, value + ".pub"]
        elif kind == "ssh-agent" and os.path.isabs(value):
            out.append(value)
    out += [str(d) for d in (owner.get("config_dirs") or {}).values() if d and os.path.isabs(str(d))]
    return out


def pinned_paths(cfg: dict) -> list[str]:
    """The credential paths pinned by every drive job with an owner record on this host.
    LaunchIsolationError when they cannot be read (rr6: a launch is never started with a pin unknown)."""
    from swarm import enrolment
    from swarm.board.autoinit import store_key
    from swarm.supervisor import ledger
    out: list[str] = []
    try:
        bk = store_key(cfg)
        found = ledger.unfinished(bk)
        jobs = [x for x in found if isinstance(x, ledger.Ledger)]
        if any(not isinstance(x, ledger.Ledger) for x in found):
            raise LaunchIsolationError("a drive ledger on this host cannot be read, so its pinned credentials "
                                     "are unknown")
        for led in jobs:
            rec = enrolment.find_job_owner(bk, led.job, strict=True)
            if rec is not None and rec.owner:
                out += _pin_paths(rec.owner)
    except LaunchIsolationError:
        raise
    except Exception as exc:
        raise LaunchIsolationError(f"the pinned credentials of this host's drive jobs cannot be read "
                                 f"({type(exc).__name__}: {exc}); not started") from exc
    return out


def platform_cover(platform: str | None = None) -> tuple:
    """Home-relative credential paths of `platform` (sys.platform) a platform jail must hide."""
    platform = platform or sys.platform
    if platform == "darwin":
        return MACOS_COVER
    if platform.startswith("win"):
        return WINDOWS_COVER
    return ()


def _under(path: str, root: str) -> bool:
    """Whether `path` is `root` or below it, compared as the platform compares paths (Windows: separators and
    case normalised, so a drive letter or a `/` from a .git file still matches)."""
    p = os.path.normcase(os.path.normpath(str(path)))
    r = os.path.normcase(os.path.normpath(str(root)))
    return p == r or p.startswith(r.rstrip(os.sep) + os.sep)


def git_dirs(cfg: dict, workdir: str) -> tuple[str | None, str | None]:
    """(git dir, common dir) of the work tree, both checked: real directories under an allowed work root
    ([supervise] allowed_workdirs) that look like git dirs. The .git file is the launch's to write, so a
    gitdir line pointing anywhere else (~/.ssh, ~) is refused, never bound. (None, None): not a repository."""
    from swarm.supervisor.settings import settings
    roots = [os.path.realpath(os.path.expanduser(r)) for r in settings(cfg)["allowed_workdirs"]]
    dotgit = os.path.join(workdir, ".git")
    if os.path.isdir(dotgit) and not os.path.islink(dotgit):
        gitdir = os.path.realpath(dotgit)
    elif os.path.isfile(dotgit) and not os.path.islink(dotgit):
        text = (_read_person(Path(dotgit)) or b"").decode("utf-8", "replace").strip()
        if not text.startswith("gitdir:"):
            raise LaunchIsolationError(f"{dotgit} is not a gitdir file")
        gitdir = os.path.realpath(os.path.join(workdir, text[len("gitdir:"):].strip()))
    else:
        return None, None
    common = gitdir
    cd = _read_person(Path(gitdir) / "commondir")
    if cd is not None:
        common = os.path.realpath(os.path.join(gitdir, cd.decode("utf-8", "replace").strip()))
    for d in (gitdir, common):
        if not any(_under(d, r) for r in roots):
            raise LaunchIsolationError(f"the work tree's git dir {d} is not under [supervise] allowed_workdirs; "
                                     "not bound into the launch")
        if not (os.path.isdir(os.path.join(d, "objects")) or os.path.isfile(os.path.join(d, "commondir"))) \
                or not os.path.exists(os.path.join(d, "HEAD")):
            raise LaunchIsolationError(f"{d} is not a git dir")
    if gitdir != common and not _under(gitdir, os.path.join(common, "worktrees")):
        raise LaunchIsolationError(f"the work tree's git dir {gitdir} is not a worktree of {common}")
    return gitdir, common


def _path_dirs(home: str) -> list[str]:
    out = []
    for p in (os.environ.get("PATH") or "").split(":"):
        p = os.path.normpath(p) if p else ""
        if p and _under(p, home) and p != home and os.path.isdir(p) and p not in out:
            out.append(p)
    return out


HARNESS_EXES = ("claude", "codex", "node")


def _exe_dirs(home: str, names) -> list[str]:
    """The install dirs (below the home) of the harness executables found on PATH: a ~/.local/bin/claude
    link points into ~/.local/share/claude/versions, which the launch must read to run it."""
    import shutil
    out = []
    for name in names:
        found = shutil.which(name) if name and "/" not in name else name
        if not found:
            continue
        real = os.path.realpath(found)
        d = os.path.dirname(real)
        if _under(real, home) and os.path.isfile(real) and d not in out:
            out.append(d)
    return out


def jail_spec(cfg: dict, launch_home: Path, workdir: str | None, home: Path | None = None,
              exes=(), read_only: bool = False) -> dict:
    """The jail of one launch. `read_only` (a reviewer or judge launch): its work tree and git dir are bound
    READ-ONLY, so whatever it runs cannot change the tree, commit or push from it (writable only for the roles
    that write: engineer, integrator, lead, ...); tools keep their temp files in the launch's private /tmp."""
    home_s = str(home or paths.home())
    h, sh = home_s, str(launch_home)
    binds: list = [[f"{sh}/claude", f"{h}/.claude", "rw"]]
    binds += [[f"{h}/.claude/{n}", f"{h}/.claude/{n}", "ro", "follow"] for n in CLAUDE_RO
              if n != "settings.json" and os.path.lexists(f"{h}/.claude/{n}")]
    binds.append([f"{sh}/{LAUNCH_SETTINGS}", f"{h}/.claude/settings.json", "ro"])   # the person's + launch allow rules
    binds += [[f"{h}/.claude/{n}", f"{h}/.claude/{n}", "rw"] for n in CLAUDE_RW if os.path.exists(f"{h}/.claude/{n}")]
    binds.append([f"{sh}/codex", f"{h}/.codex", "rw"])
    binds += [[f"{h}/.codex/{n}", f"{h}/.codex/{n}", "ro", "follow"] for n in CODEX_RO
              if os.path.lexists(f"{h}/.codex/{n}")]
    binds += [[f"{h}/.codex/{n}", f"{h}/.codex/{n}", "rw"] for n in CODEX_RW if os.path.exists(f"{h}/.codex/{n}")]
    binds += [[f"{sh}/.ssh", f"{h}/.ssh", "ro"], [f"{sh}/gitconfig", f"{h}/.gitconfig", "ro"],
              [f"{sh}/broker", f"{h}/.swarm-launch/broker", "rw"], [f"{sh}/bin", f"{h}/.swarm-launch/bin", "ro"]]
    ro = [p for p in glob.glob(f"{h}/.local/share/swarm/venv*") if os.path.isdir(p)]
    ro += _path_dirs(h) + _exe_dirs(h, (*exes, *HARNESS_EXES)) + [f"{h}/{p}" for p in DEFAULT_RO]
    ro += _list(cfg, "launch_ro_paths")
    from swarm.supervisor.settings import settings
    plugin_dir = settings(cfg).get("claude_plugin_dir")
    if plugin_dir:
        ro.append(os.path.normpath(os.path.expanduser(str(plugin_dir))))
    key = _drive(cfg).get("launch_ssh_key")
    if key:
        ro.append(os.path.normpath(str(key)))
    seen = {b[1] for b in binds}
    for p in ro:
        if os.path.exists(p) and p not in seen:
            binds.append([p, p, "ro", "follow"])
            seen.add(p)
    rw = list(_list(cfg, "launch_rw_paths"))
    tree: list = []
    overlays = []
    if workdir:
        tree.append(workdir)
        gitdir, common = git_dirs(cfg, workdir)
        if common and not _under(common, workdir):
            tree.append(common)
        for d in {x for x in (gitdir, common) if x}:
            overlays += [os.path.join(d, n) for n in ("hooks", "config", "config.worktree")]
    for p in tree:
        if os.path.exists(p) and p not in seen:
            binds.append([p, p, "ro" if read_only else "rw"])
            seen.add(p)
    for p in rw:
        if os.path.exists(p) and p not in seen:
            binds.append([p, p, "rw"])
            seen.add(p)
    for p in overlays:     # after the work tree and git dir: their hooks and config read-only
        if os.path.exists(p):
            binds.append([p, p, "ro"])
    cover = [p for p in pinned_paths(cfg) + _list(cfg, "launch_cover_paths") if not _under(p, h)]
    return {"home": h, "home_base": f"{sh}/home", "tmp": f"{sh}/tmp", "binds": binds, "cover": cover,
            "runtime_dir": f"/run/user/{os.getuid()}" if hasattr(os, "getuid") else None, "cwd": workdir or h,
            "path_prepend": [f"{h}/.swarm-launch/bin"],
            "env": {"GIT_CONFIG_GLOBAL": f"{h}/.gitconfig", "GIT_SSH_COMMAND": f"ssh -F {h}/.ssh/config",
                    "SWARM_LAUNCH_BROKER": f"{h}/.swarm-launch/broker", **_jail_git_overrides(h),
                    **(READ_ONLY_ENV if read_only else {})}}


# A read-only launch runs the tests read-only: no bytecode next to the sources, no pytest cache in the tree.
READ_ONLY_ENV = {"PYTHONDONTWRITEBYTECODE": "1", "PYTEST_ADDOPTS": "-p no:cacheprovider"}


def best_effort_env(cfg: dict, launch_home: Path, platform: str | None = None) -> dict:
    """What a best-effort launch (no filesystem jail: macOS, Windows) runs with on top of the allowlisted
    environment (launchjail.scrub_env): no system git config and a launch gitconfig with an empty credential helper,
    ssh with no agent and no default identity, an empty gh config dir, a scratch HOME (the harness keeps its
    own config dir), and the launch's broker as its only way to the board."""
    h = str(launch_home)
    real = str(paths.home())
    scratch = os.path.join(h, "home")
    ssh_cfg = os.path.join(h, ".ssh", "config")
    from swarm import launchjail
    ssh = f'ssh -F "{ssh_cfg}" -o IdentityAgent=none -o IdentitiesOnly=yes -o IdentityFile=none -o BatchMode=yes'
    env = {"HOME": scratch, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.path.join(h, "gitconfig"),
           "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never", "GIT_SSH_COMMAND": ssh,
           **launchjail.git_overrides(ssh),
           "GH_CONFIG_DIR": os.path.join(h, "gh"), "SWARM_LAUNCH_BROKER": os.path.join(h, "broker"),
           "CLAUDE_CONFIG_DIR": os.path.join(real, ".claude"), "CODEX_HOME": os.path.join(real, ".codex"),
           "SWARM_LAUNCH_ISOLATION": "best-effort"}
    if (platform or sys.platform).startswith("win"):
        env.update({"USERPROFILE": scratch, "APPDATA": os.path.join(scratch, "AppData", "Roaming"),
                    "LOCALAPPDATA": os.path.join(scratch, "AppData", "Local")})
    return env


def best_effort_child_env(base: dict, overrides: dict, launch_home: str, platform: str | None = None) -> dict:
    """A best-effort launch's whole environment: the allowlist of `base`, the overrides, the launch's `swarm` first
    on PATH."""
    from swarm import launchjail
    out = launchjail.scrub_env(dict(base), platform)
    out.update(overrides)
    out["PATH"] = os.pathsep.join([os.path.join(launch_home, "bin"), out.get("PATH", "")]).rstrip(os.pathsep)
    return out


BEST_EFFORT_NOTE = "best-effort on {platform} (no filesystem jail)"


last_fallback_reason: list = [None]   # why the last Linux launch fell back (the runner logs it)


def launch(cfg: dict, argv: list, *, job: str, launch: str, workdir: str | None = None, read_only: bool = False,
           platform: str | None = None, probe=None, no_userns=None) -> tuple[list, dict | None, str]:
    """How a drive launch starts: (argv, env overrides or None, mode). Linux: the jail (wrap; fail closed).
    macOS/Windows: best-effort (argv unchanged, best_effort_env). "off" (a person's act): unchanged, nothing."""
    m = mode(cfg, platform)
    if m == "off":
        return list(argv), None, m
    if m == "required":
        from swarm import launchjail
        ok, why = (probe or launchjail.available)()
        if ok:
            return wrap(cfg, argv, job=job, launch=launch, workdir=workdir, probe=lambda: (True, ""),
                        read_only=read_only), None, m
        if strict(cfg):
            raise LaunchIsolationError(f"this host cannot jail a drive launch ({why}) and [drive] launch_isolation = "
                                     "\"strict\": not started")
        # Fall back ONLY when this host deterministically has no unprivileged user namespaces. Any other failure
        # (a timeout, ENOSPC, EAGAIN, ENOMEM: things a launch can cause) refuses: never a downgrade.
        reason = (no_userns or launchjail.no_userns_reason)()
        if not reason:
            raise LaunchIsolationError(f"the launch jail could not be built ({why}) on a host that supports it: not "
                                     "started (fail closed; retried by the driver)")
        m = "best-effort"
        last_fallback_reason[0] = reason
    elif strict(cfg):
        raise LaunchIsolationError(f"no filesystem jail on {platform or sys.platform} and [drive] launch_isolation = "
                                 "\"strict\": not started")
    h = prepare(cfg, job, launch)
    return with_launch_settings(argv, h), best_effort_env(cfg, h, platform), m


def with_launch_settings(argv: list, launch_home: Path) -> list:
    """A best-effort Claude launch keeps the person's config dir (CLAUDE_CONFIG_DIR), which is not the launch's to
    write: its allow rules come as `--settings <the launch's settings file>` (merged with the user settings)."""
    argv = list(argv)
    if argv and os.path.basename(str(argv[0])).lower().startswith("claude") and "--settings" not in argv:
        argv[1:1] = ["--settings", os.path.join(str(launch_home), LAUNCH_SETTINGS)]
    return argv


def _jail_git_overrides(h: str) -> dict:
    from swarm import launchjail
    return launchjail.git_overrides(f"ssh -F {h}/.ssh/config")


def jail_script() -> str:
    return str(Path(__file__).resolve().parent.parent / "launchjail.py")


def wrap(cfg: dict, argv: list, *, job: str, launch: str, workdir: str | None = None, python: str | None = None,
         read_only: bool = False,
         probe=None) -> list:
    """The drive launch's argv, jailed: [python, -I, launchjail.py, SPEC, --, *argv]. Unchanged only when a person
    turned isolation off. LaunchIsolationError when this host cannot jail a launch (`probe()`, default
    launchjail.available, says why) or its view cannot be built: the launch is never started unjailed."""
    if mode(cfg) == "off":
        return list(argv)
    from swarm import launchjail
    ok, why = (probe or launchjail.available)()
    if not ok:
        raise LaunchIsolationError(f"this host cannot isolate a drive launch from the person's files ({why}); "
                                 "not started. Enable unprivileged user namespaces, or have a person run "
                                 "`swarm supervise launch-isolation off` to accept launches that can read them")
    h = prepare(cfg, job, launch)
    spec = jail_spec(cfg, h, workdir, exes=(str(argv[0]),) if argv else (), read_only=read_only)
    return [python or sys.executable, "-I", jail_script(), json.dumps(spec, separators=(",", ":")), "--",
            *[str(a) for a in argv]]


# --- the doctor probe -------------------------------------------------------------------------------

PROBE = r"""
fail=0
for p in "$@"; do
  if [ -f "$p" ] && [ -s "$p" ]; then echo "READABLE $p"; fail=1; fi
  if [ -d "$p" ] && [ -n "$(ls -A "$p" 2>/dev/null)" ]; then echo "LISTABLE $p"; fail=1; fi
done
for k in "$HOME"/.ssh/id_* ; do [ -e "$k" ] && { echo "KEY $k"; fail=1; }; done
[ -n "$SSH_AUTH_SOCK$GH_TOKEN$GITHUB_TOKEN$PGPASSWORD" ] && { echo "ENV credential variable set"; fail=1; }
h=$(git config --get-all credential.helper 2>/dev/null | tr -d '[:space:]')
[ -n "$h" ] && { echo "HELPER git credential helper configured"; fail=1; }
if command -v gh >/dev/null 2>&1; then gh auth status >/dev/null 2>&1 && { echo "GH gh is logged in"; fail=1; }; fi
for s in /tmp/tmux-* /tmp/ssh-* ; do [ -e "$s" ] && { echo "SOCKET $s"; fail=1; }; done
if [ "${SWARM_PROBE_NETWORK:-0}" = 1 ] && command -v ssh >/dev/null 2>&1; then
  out=$(timeout 20 ssh -F "$HOME/.ssh/config" -T -o ConnectTimeout=10 git@github.com 2>&1)
  case "$out" in *"successfully authenticated"*) echo "SSH github accepts a key"; fail=1;; esac
fi
[ $fail = 0 ] && echo LAUNCH-CLEAN
exit $fail
"""

# Probed by path from inside the jail (existence and size only, never contents).
PROBE_PATHS = (".config", ".config/gh", ".secrets", ".git-credentials", ".netrc", ".bash_history",
               ".local/share/swarm/supervisor", ".pg0", ".local/share/pki", ".config/swarm")


def probe(cfg: dict, *, network: bool = False, run=None, available=None, no_userns=None) -> tuple[bool | None, str]:
    """Run PROBE in a launch jail. (True, detail) when no credential is reachable; (False, what is
    reachable); (None, why) when a person turned isolation off or this host cannot jail a launch."""
    import subprocess
    m = mode(cfg)
    if m == "off":
        return None, "off (a person ran `swarm supervise launch-isolation off`): drive launches can read the person's files"
    if m == "best-effort":
        if strict(cfg):
            return None, f"unavailable: no filesystem jail on {sys.platform} ([drive] launch_isolation = \"strict\")"
        return None, BEST_EFFORT_NOTE.format(platform=sys.platform)
    from swarm import launchjail
    ok, why = (available or launchjail.available)()
    if not ok:
        reason = None if strict(cfg) else (no_userns or launchjail.no_userns_reason)()
        if not reason:
            return None, f"unavailable: {why}"
        return None, BEST_EFFORT_NOTE.format(platform=sys.platform) + f": {reason}"
    home = str(paths.home())
    targets = [f"{home}/{p}" for p in PROBE_PATHS] + pinned_paths(cfg)
    argv = wrap(cfg, ["sh", "-c", PROBE, "probe", *targets], job="_doctor", launch="probe",
                probe=lambda: (True, ""))
    env = dict(os.environ)
    env["SWARM_PROBE_NETWORK"] = "1" if network else "0"
    try:
        res = (run or subprocess.run)(argv, capture_output=True, text=True, timeout=60, env=env,
                                      cwd=paths.state_dir() if paths.state_dir().is_dir() else "/")
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"probe failed to run ({type(exc).__name__})"
    lines = [x for x in (res.stdout or "").splitlines() if x.strip()]
    if res.returncode == 0 and "LAUNCH-CLEAN" in lines:
        return True, "active: no key, token, agent, socket or credential helper reachable from a launch"
    return False, "; ".join(lines[:6]) or f"probe exit {res.returncode}"
