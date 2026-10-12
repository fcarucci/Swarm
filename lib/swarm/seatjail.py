"""The seat boundary (Task 20, spec 4.0): a drive seat runs in its own user, mount and pid namespaces and
sees an ALLOWLIST of the person's files, nothing else.

A drive seat (Claude or Codex, every role) runs as the person's unix user, so a changed $HOME alone is no
boundary (ssh finds the key through the password database; any process opens an absolute path) and a
denylist of credential paths misses the next secret. Inside the jail:

- $HOME is the seat's own directory (on disk, in the supervisor's private dir), and /tmp is the seat's own
  too: nothing of the person's home, and no socket of the person's /tmp (tmux, herdr, ssh agents, IDE
  servers), is there unless the spec binds it back;
- the spec binds back exactly what the seat needs: its work tree and git dir (writable; the git hooks and
  config read-only), the harness's login and settings (read-only, except the login file a token refresh
  writes), the swarm code and the tools on PATH (read-only), the seat's .ssh (known_hosts only), and the
  seat's broker directory (the only way to the board: swarm.supervisor.seatbroker);
- /run/user/<uid> (the systemd user manager, dbus, agent sockets) and /dev/shm are empty tmpfs, and every
  `cover` path (pinned credentials outside the home) is covered;
- a fresh /proc of the jail's own pid namespace: no /proc/<pid>/root or environ of a process outside;
- the mounts are made in a first user namespace and the seat runs in a second one nested in it, so they are
  locked (the kernel refuses to unmount them) and read-only binds cannot be remounted writable; the seat runs
  with no capability (a non-root uid after execve) and no_new_privs;
- the environment is an allowlist (seat_env): the locale and terminal, PATH, the seat's swarm run variables
  and the harness's model API variables; nothing else.

A host that cannot make the namespaces (not Linux; unprivileged user namespaces disabled) never runs a seat
unjailed: the jail exits EXIT_UNAVAILABLE before it execs anything. `available()` says beforehand.

This file imports the standard library only: it is run as `python -I <this file> SPEC -- argv...`."""
from __future__ import annotations

import ctypes
import json
import os
import re
import signal
import stat
import sys

EXIT_UNAVAILABLE = 125
JAIL_ENV = "SWARM_SEAT_JAIL"      # set to "1" inside the jail (probes and doctor read it)

CLONE_NEWNS = 0x00020000
CLONE_NEWUSER = 0x10000000
CLONE_NEWPID = 0x20000000
MS_RDONLY, MS_NOSUID, MS_NODEV, MS_NOEXEC = 1, 2, 4, 8
MS_REMOUNT, MS_BIND, MS_NOATIME, MS_NODIRATIME = 32, 4096, 1024, 2048
MS_REC, MS_PRIVATE, MS_RELATIME = 16384, 1 << 18, 1 << 21
PR_SET_NO_NEW_PRIVS = 38
# statvfs f_flag bits -> the mount flags a read-only remount must keep (locked flags in a user namespace)
_KEEP_FLAGS = ((2, MS_NOSUID), (4, MS_NODEV), (8, MS_NOEXEC), (1024, MS_NOATIME), (2048, MS_NODIRATIME),
               (4096, MS_RELATIME))

# The environment a seat keeps (rr5: an allowlist). Exact names, plus LC_*.
KEEP_ENV = frozenset((
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LANGUAGE", "TERM", "TZ", "COLORTERM", "NO_COLOR",
    # the seat's swarm run variables the runner sets
    "SWARM_RUN_ID", "SWARM_RESUME_TOKEN",
    # the harness's model API (its own login, not a repository credential)
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL",
    "ANTHROPIC_SMALL_FAST_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
    "DISABLE_AUTOUPDATER", "DISABLE_TELEMETRY", "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_ORG_ID",
    # what a process needs to run at all on Windows (best-effort seats)
    "SYSTEMROOT", "SystemRoot", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "SYSTEMDRIVE", "PROCESSOR_ARCHITECTURE",
    "NUMBER_OF_PROCESSORS", "OS"))


def scrub_env(env: dict) -> dict:
    """The allowlisted part of `env` (KEEP_ENV and LC_*): every other name is dropped, whatever
    [supervise] pass_env says (PGPASSWORD, AWS_*, DATABASE_URL, TMUX, HERDR_SOCKET_PATH, *_TOKEN, ...)."""
    return {k: v for k, v in env.items() if k in KEEP_ENV or re.fullmatch(r"LC_[A-Z_]+", k)}


def seat_env(env: dict, spec: dict) -> dict:
    """The environment a jailed seat execs with: scrub_env, then the spec's own variables (git and ssh
    pointed at the seat's config, the broker directory, the seat's PATH)."""
    out = scrub_env(env)
    out.update({JAIL_ENV: "1", "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
                "GCM_INTERACTIVE": "never", "TMPDIR": "/tmp"})
    for k, v in (spec.get("env") or {}).items():
        if isinstance(k, str) and isinstance(v, str):
            out[k] = v
    if spec.get("path_prepend"):
        out["PATH"] = ":".join([*spec["path_prepend"], out.get("PATH", "/usr/bin:/bin")])
    if spec.get("home"):
        out["HOME"] = spec["home"]
    return out


# --- the namespaces --------------------------------------------------------------------------------

_libc = None


def _lib():
    global _libc
    if _libc is None:
        _libc = ctypes.CDLL(None, use_errno=True)
    return _libc


def _check(rc: int, what: str) -> None:
    if rc != 0:
        err = ctypes.get_errno()
        raise OSError(err, f"{what}: {os.strerror(err)}")


def _mount(src, dst, fstype, flags, data=None) -> None:
    enc = (lambda s: None if s is None else os.fsencode(s))
    _check(_lib().mount(enc(src), enc(dst), enc(fstype), ctypes.c_ulong(flags), enc(data)), f"mount {dst}")


def _unshare(flags: int) -> None:
    if hasattr(os, "unshare"):
        os.unshare(flags)
    else:
        _check(_lib().unshare(ctypes.c_int(flags)), "unshare")


def _write(path: str, text: str) -> None:
    fd = os.open(path, os.O_WRONLY)
    try:
        os.write(fd, text.encode())
    finally:
        os.close(fd)


def _map_ids(inside_uid: int, outside_uid: int, inside_gid: int, outside_gid: int) -> None:
    try:
        _write("/proc/self/setgroups", "deny")
    except FileNotFoundError:
        pass
    _write("/proc/self/uid_map", f"{inside_uid} {outside_uid} 1\n")
    _write("/proc/self/gid_map", f"{inside_gid} {outside_gid} 1\n")


def _cover(path: str, dev_null: str = "/dev/null") -> bool:
    """Cover one path: a directory by an empty read-only tmpfs, anything else by /dev/null."""
    try:
        st = os.stat(path)
    except OSError:
        return False
    if stat.S_ISDIR(st.st_mode):
        _mount("tmpfs", path, "tmpfs", MS_RDONLY | MS_NOSUID | MS_NODEV | MS_NOEXEC, "mode=0500,size=16k")
    else:
        _mount(dev_null, path, None, MS_BIND)
    return True


def _readonly(path: str) -> None:
    """Remount the bind at `path` read-only, keeping the flags a user namespace locks."""
    flags = MS_REMOUNT | MS_BIND | MS_RDONLY
    have = os.statvfs(path).f_flag
    for bit, mflag in _KEEP_FLAGS:
        if have & bit:
            flags |= mflag
    _mount(None, path, None, flags)


def _mountpoint(dst: str, is_dir: bool, writable_roots: tuple) -> None:
    """Make `dst` a mount point of the right kind. Below a seat-writable root (its own home or /tmp) a
    symbolic link on the way is removed first: a link the seat planted never redirects a bind."""
    parts = dst.strip("/").split("/")
    cur = ""
    for i, part in enumerate(parts):
        cur += "/" + part
        last = i == len(parts) - 1
        seat_owned = any(cur.startswith(r + "/") for r in writable_roots)
        try:
            st = os.lstat(cur)
        except FileNotFoundError:
            st = None
        if st is not None and stat.S_ISLNK(st.st_mode) and seat_owned:
            os.unlink(cur)
            st = None
        if st is None:
            if last and not is_dir:
                os.close(os.open(cur, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600))
            else:
                os.mkdir(cur, 0o700)


def _forward_and_wait(child: int) -> int:
    """Forward the usual stop signals to `child`, reap everything, return the child's exit status."""
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGQUIT, signal.SIGUSR1, signal.SIGUSR2):
        try:
            signal.signal(sig, lambda n, _f: _kill(child, n))
        except (OSError, ValueError):
            pass
    status = 1
    while True:
        try:
            pid, st = os.waitpid(-1, 0)
        except ChildProcessError:
            return status
        except InterruptedError:
            continue
        if pid == child:
            status = os.waitstatus_to_exitcode(st)
            status = 128 - status if status < 0 else status


def _kill(pid: int, sig: int) -> None:
    try:
        os.kill(pid, sig)
    except OSError:
        pass


def _open_source(src: str, follow: bool) -> int | None:
    flags = os.O_PATH | os.O_CLOEXEC | (0 if follow else os.O_NOFOLLOW)
    try:
        return os.open(src, flags)
    except OSError:
        return None


def setup_mounts(spec: dict, uid: int) -> list[str]:
    """In the first namespace (root there): private propagation, a fresh /proc, the seat's home over the
    person's and its /tmp over /tmp, the runtime dir and /dev/shm emptied, then the allowlisted binds
    (sources opened before anything is covered), the covers, and last the read-only remounts. The paths
    mounted, in order."""
    _mount(None, "/", None, MS_REC | MS_PRIVATE)
    _mount("proc", "/proc", "proc", MS_NOSUID | MS_NODEV | MS_NOEXEC)
    binds = []
    for b in spec.get("binds", ()):
        src, dst, mode = b[0], b[1], b[2]
        follow = len(b) > 3 and b[3] == "follow"
        fd = _open_source(src, follow)
        if fd is not None:
            binds.append((fd, dst, mode))
    base_fds = {}
    for key in ("home_base", "tmp"):
        if spec.get(key):
            fd = _open_source(spec[key], False)
            if fd is None:
                raise OSError(2, f"the seat's {key} {spec[key]} is missing")
            base_fds[key] = fd
    done = []
    home = spec.get("home")
    roots = []
    if "tmp" in base_fds:
        _mount(f"/proc/self/fd/{base_fds['tmp']}", "/tmp", None, MS_BIND)
        done.append("/tmp")
        roots.append("/tmp")
    if home and "home_base" in base_fds:
        _mountpoint(home, True, tuple(roots))
        _mount(f"/proc/self/fd/{base_fds['home_base']}", home, None, MS_BIND)
        done.append(home)
        roots.append(home)
    runtime = spec.get("runtime_dir") or f"/run/user/{uid}"
    for d, opts in ((runtime, "mode=0700,size=1m"), ("/dev/shm", "mode=1777,size=64m")):
        if os.path.isdir(d):
            _mount("tmpfs", d, "tmpfs", MS_NOSUID | MS_NODEV, opts)
            done.append(d)
    readonly = []
    for fd, dst, mode in binds:
        is_dir = stat.S_ISDIR(os.fstat(fd).st_mode)
        _mountpoint(dst, is_dir, tuple(roots))
        _mount(f"/proc/self/fd/{fd}", dst, None, MS_BIND | MS_REC)
        done.append(dst)
        if mode == "ro":
            readonly.append(dst)
    for path in spec.get("cover", ()):
        if path not in done and _cover(path):
            done.append(path)
    for dst in readonly:
        _readonly(dst)
    for fd in [b[0] for b in binds] + list(base_fds.values()):
        os.close(fd)
    return done


def run(spec: dict, argv: list[str]) -> int:
    """Run `argv` in the jail; its exit status (EXIT_UNAVAILABLE when the jail cannot be made: nothing ran)."""
    if not argv:
        print("seatjail: no command", file=sys.stderr)
        return 2
    uid, gid = os.getuid(), os.getgid()
    try:
        _unshare(CLONE_NEWUSER | CLONE_NEWNS | CLONE_NEWPID)
        _map_ids(0, uid, 0, gid)
    except OSError as exc:
        print(f"seatjail: cannot isolate the seat ({exc}); not started", file=sys.stderr)
        return EXIT_UNAVAILABLE
    init = os.fork()
    if init:                                   # outside the pid namespace: wait for the jail's init
        return _forward_and_wait(init)
    try:                                       # pid 1 of the jail, root of the first user namespace
        setup_mounts(spec, uid)
    except OSError as exc:
        os.write(2, f"seatjail: cannot build the seat's file view ({exc}); not started\n".encode())
        os._exit(EXIT_UNAVAILABLE)
    seat = os.fork()
    if seat:
        os._exit(_forward_and_wait(seat))
    try:                                       # the seat: a nested user namespace locks the mounts
        _unshare(CLONE_NEWUSER | CLONE_NEWNS)
        _map_ids(uid, 0, gid, 0)
        _check(_lib().prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0), "no_new_privs")
        env = seat_env(dict(os.environ), spec)
        # the inherited cwd and any inherited descriptor (the runner's work-dir descriptor) still point into
        # the person's file system below the covers: the seat starts from its own view, with only 0-2 open
        os.chdir(spec.get("cwd") or spec.get("home") or "/")
        os.closerange(3, 65536)
        os.execvpe(argv[0], argv, env)
    except OSError as exc:
        os.write(2, f"seatjail: cannot start {argv[0]} in the jail ({exc})\n".encode())
        os._exit(EXIT_UNAVAILABLE)


def available(python: str | None = None, timeout: float = 10.0) -> tuple[bool, str]:
    """Whether this host can jail a seat: run `true` in the jail. (ok, why not)."""
    import subprocess
    if not sys.platform.startswith("linux"):
        return False, f"no user namespaces on {sys.platform}"
    try:
        res = subprocess.run([python or sys.executable, "-I", os.path.abspath(__file__), "{}", "--", "true"],
                             capture_output=True, text=True, timeout=timeout, env=scrub_env(dict(os.environ)))
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if res.returncode == 0:
        return True, ""
    return False, (res.stderr.strip().splitlines() or [f"exit {res.returncode}"])[-1][:300]


def main(args: list[str]) -> int:
    if len(args) < 3 or args[1] != "--":
        print("usage: seatjail.py SPEC_JSON -- COMMAND [ARG...]", file=sys.stderr)
        return 2
    try:
        spec = json.loads(args[0])
        if not isinstance(spec, dict):
            raise ValueError("not an object")
    except ValueError as exc:
        print(f"seatjail: bad spec ({exc})", file=sys.stderr)
        return 2
    return run(spec, args[2:])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
