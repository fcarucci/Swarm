"""The Windows hook entry: what bin/swarm-hook.cmd (and bin/swarm-hook under Git Bash) runs. The
Python port of the sh script bin/swarm-hook: `swarm-hook [--host H] start|turn|done|stop|session-start`
with the hook JSON on stdin. Never fails the agent and never builds anything: session-start only
starts a detached `swarm bootstrap` when this plugin version hasn't been set up yet on an existing
install (a new machine gets a "run `swarm init`" notice); the others
exit before the swarm package is imported unless a swarm job marker exists.

Files: only the host-only directory ~/.local/share/swarm/host is written or read here (stamps,
bootstrap log, the hooks' stderr, notices). On Windows there is no 0700 to check: the directory
sits in the user's profile, whose NTFS ACL already keeps other users out; a symlink or junction
there is refused. Stdlib only."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN_ROOT = HERE.parent.parent
try:
    from swarm import winlaunch
except ImportError:   # run as a script: lib/swarm/winhook.py, the swarm package is not importable yet
    sys.path.append(str(HERE))
    import winlaunch  # noqa: E402


def _home() -> Path:
    return Path(os.path.expanduser("~"))


def _is_link(p: Path) -> bool:
    if p.is_symlink():
        return True
    try:   # NTFS junctions are not symlinks to Python 3.11
        return bool(os.lstat(p).st_file_attributes & 0x400)   # FILE_ATTRIBUTE_REPARSE_POINT
    except (OSError, AttributeError):
        return False


def private_dir() -> Path | None:
    """~/.local/share/swarm/host, created if missing, or None when it can't safely be used."""
    hd = _home() / ".local" / "share" / "swarm" / "host"
    try:
        if not hd.exists() and not _is_link(hd):
            hd.mkdir(parents=True, exist_ok=True)
        return hd if hd.is_dir() and not _is_link(hd) else None
    except OSError:
        return None


def plugin_version(root: Path | None = None) -> str:
    root = root or PLUGIN_ROOT
    try:
        v = str(json.loads((root / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["version"])
    except (OSError, ValueError, KeyError):
        return "0"
    return v if re.fullmatch(r"[A-Za-z0-9._+-]*", v) else "0"


def marker_dir() -> Path:
    cfg = Path(os.environ.get("SWARM_CONFIG") or (_home() / ".config" / "swarm" / "config.toml"))
    md = ""
    try:
        m = re.search(r'(?m)^[ \t]*marker_dir[ \t]*=[ \t]*"(.*)".*$', cfg.read_text(encoding="utf-8"))
        md = m.group(1) if m else ""
    except OSError:
        pass
    md = md or str(_home() / ".local" / "state" / "swarm" / "active")
    return Path(os.path.expanduser(md)) if md.startswith("~") else Path(md)


def _detached(cmd: list[str], log: Path) -> None:
    flags = 0x00000008 | 0x00000200 | 0x08000000   # DETACHED_PROCESS | NEW_PROCESS_GROUP | NO_WINDOW
    with open(log, "ab") as out:
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=out, stderr=out, close_fds=True,
                         creationflags=flags if os.name == "nt" else 0, env=winlaunch.run_env(PLUGIN_ROOT))


NOT_SET_UP = ("[swarm] not set up: run `swarm init` (the plugin's bin/swarm). It creates ~/.config/swarm/config.toml, "
              "the launcher ~/.local/bin/swarm and the board, and edits no Claude or Codex settings file unless you "
              "pass --apply-settings; see the README. Until then the swarm hooks do nothing and nothing has been written.")


def existing_install() -> bool:
    """A machine set up before: its config, the launcher, or a bootstrap stamp of any version
    (bin/swarm-hook and swarm.bootstrap.existing_install test the same three things)."""
    cfg = Path(os.environ.get("SWARM_CONFIG") or (_home() / ".config" / "swarm" / "config.toml"))
    launcher = _home() / ".local" / "bin" / ("swarm.cmd" if os.name == "nt" else "swarm")
    return (os.path.lexists(cfg) or os.path.lexists(launcher)
            or any(p.is_file() for p in (_home() / ".local" / "share" / "swarm" / "host").glob("bootstrap-*")))


def session_start(host: str, priv: Path | None) -> int:
    sys.stdin.read()
    if priv is None:
        msg = ("[swarm] setup problem: ~/.local/share/swarm/host is not a usable (non-symlink) directory "
               "of yours, so the swarm hooks do nothing. See `swarm doctor`.")
        print(json.dumps({"systemMessage": msg}))
        print(msg, file=sys.stderr)
        return 0
    ver = plugin_version() or "0"
    key = str(zlib.crc32(str(PLUGIN_ROOT).encode()))
    h = host or "unknown"
    (priv / f"hooks-ran-{h}-{ver}-{key}").touch()
    notices = priv / f"notices-{h}.json"
    vpy = winlaunch.venv_python(winlaunch.venv_dir())
    if host and notices.is_file() and not _is_link(notices) and vpy.exists():
        with open(priv / "hook-stderr.log", "ab") as err:
            subprocess.run([str(vpy), "-B", "-m", "swarm.cli", "notices", "--hook-output", "--host", host],
                           stdin=subprocess.DEVNULL, stderr=err, env=winlaunch.run_env(PLUGIN_ROOT))
    stamp = priv / f"bootstrap-{h}-{ver}-{key}"
    if stamp.is_file():
        return 0
    args = ["bootstrap", *(["--host", host] if host else []), "--quiet", "--stamp", str(stamp)]
    _detached([sys.executable, str(HERE / "winlaunch.py"), *args], priv / "bootstrap.log")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    host = ""
    if argv and argv[0] == "--host":
        if len(argv) < 3:
            sys.stdin.read()
            return 0
        host, argv = argv[1], argv[2:]
    if not argv or not re.fullmatch(r"[A-Za-z0-9_-]*", host):
        sys.stdin.read()
        return 0
    event = argv[0]
    try:
        if event == "session-start" and not existing_install():   # a new machine: say so, write nothing
            sys.stdin.read()
            print(json.dumps({"systemMessage": NOT_SET_UP}))
            print(NOT_SET_UP, file=sys.stderr)
            return 0
        priv = private_dir()
        if event == "session-start":
            return session_start(host, priv)
        md = marker_dir()
        if not (host == "codex" and event == "session-end") and (not md.is_dir() or not any(md.glob("*.json"))):
            sys.stdin.read()
            return 0
        vpy = winlaunch.venv_python(winlaunch.venv_dir())
        if not vpy.exists():   # never build the venv from a hook: session-start's bootstrap does that
            sys.stdin.read()
            return 0
        log = open(priv / "hook-stderr.log", "ab") if priv else subprocess.DEVNULL
        try:
            subprocess.run([str(vpy), "-B", "-m", "swarm.cli", "hook", *(["--host", host] if host else []), event],
                           stderr=log, env=winlaunch.run_env(PLUGIN_ROOT))
        finally:
            if priv:
                log.close()
    except Exception:   # a hook never fails the agent
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
