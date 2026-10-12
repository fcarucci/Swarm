"""The seat credential boundary (Task 20, spec 4.0): a drive seat runs in its own user, mount and pid
namespaces, where the person's push credentials are not there to read.

A drive seat (Claude or Codex, every role) runs as the person's unix user, so a changed $HOME alone
is no boundary: ssh finds the key through the password database, and any process can open an absolute
path. Inside the jail:

- every credential path named in the spec (the person's ~/.ssh, gh config, git credential stores,
  ~/.netrc, the pinned credentials of the host's drive jobs, ...) is covered: a directory by an empty
  read-only tmpfs, a file or socket by /dev/null. ~/.ssh is replaced by the seat's own .ssh (a
  known_hosts and a config that names no identity and no agent);
- the user's runtime directory (/run/user/<uid>: the systemd user manager, dbus, agent sockets) is an
  empty tmpfs, so `systemd-run --user` cannot start a process outside the jail;
- a fresh /proc of the jail's own pid namespace: /proc/<pid>/root of a process outside cannot be used
  to look at the person's file system;
- the covering mounts are made in a first user namespace and the seat runs in a second one nested in it,
  so they are locked: the seat cannot unmount them (the kernel refuses, MNT_LOCKED), and it runs with no
  capability (a non-root uid after execve) and no_new_privs;
- the environment is scrubbed of credential variables (scrub_env) and points git and gh at the seat's
  own config (GIT_CONFIG_GLOBAL, GH_CONFIG_DIR, no system config, no prompts).

A host that cannot make the namespaces (not Linux; unprivileged user namespaces disabled, as on an
AppArmor-restricted Ubuntu) never runs a seat unjailed: the jail exits EXIT_UNAVAILABLE before it
execs anything. `available()` says beforehand.

This file imports the standard library only: it is run as `python -I <this file> SPEC -- argv...`
(isolated mode: nothing from the work dir or the environment is imported)."""
from __future__ import annotations

import ctypes
import json
import os
import re
import signal
import sys

EXIT_UNAVAILABLE = 125
JAIL_ENV = "SWARM_SEAT_JAIL"      # set to "1" inside the jail (probes and doctor read it)

CLONE_NEWNS = 0x00020000
CLONE_NEWUSER = 0x10000000
CLONE_NEWPID = 0x20000000
MS_RDONLY, MS_NOSUID, MS_NODEV, MS_NOEXEC = 1, 2, 4, 8
MS_REMOUNT, MS_BIND, MS_REC, MS_PRIVATE = 32, 4096, 16384, 1 << 18
PR_SET_NO_NEW_PRIVS = 38

# Variables never passed to a seat, whatever [supervise] pass_env says.
SCRUB_NAMES = frozenset((
    "SSH_AUTH_SOCK", "SSH_AGENT_PID", "SSH_ASKPASS", "SSH_ASKPASS_REQUIRE", "GIT_ASKPASS", "GIT_SSH",
    "GIT_SSH_COMMAND", "GIT_SSH_VARIANT", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT", "GIT_CONFIG",
    "GIT_CONFIG_SYSTEM", "GIT_CREDENTIAL_HELPER", "GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN", "GH_HOST", "GITEA_TOKEN", "GITLAB_TOKEN", "DBUS_SESSION_BUS_ADDRESS",
    "XDG_RUNTIME_DIR", "NETRC", "GNUPGHOME"))
_SCRUB_PATTERN = re.compile(r"^(GIT_CONFIG_(KEY|VALUE)_\d+|.*_(TOKEN|PAT|PASSWORD|PASSPHRASE|CREDENTIALS?))$")
# The harness's own auth (Claude, Codex) and swarm's run bookkeeping are not push credentials.
_SCRUB_KEEP = re.compile(r"^(SWARM_|CLAUDE_|ANTHROPIC_|CODEX_|OPENAI_)")


def scrub_env(env: dict) -> dict:
    """`env` without push-credential variables (SCRUB_NAMES, *_TOKEN and the like, git config
    injection), keeping the harness's auth and swarm's own run variables."""
    out = {}
    for k, v in env.items():
        if k in SCRUB_NAMES:
            continue
        if _SCRUB_PATTERN.match(k) and not _SCRUB_KEEP.match(k):
            continue
        out[k] = v
    return out


def seat_env(env: dict, spec: dict) -> dict:
    """The environment a jailed seat execs with: scrub_env, git and gh pointed at the seat's config."""
    out = scrub_env(env)
    out.update({JAIL_ENV: "1", "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
                "GCM_INTERACTIVE": "never"})
    if spec.get("gitconfig"):
        out["GIT_CONFIG_GLOBAL"] = spec["gitconfig"]
    if spec.get("gh_config_dir"):
        out["GH_CONFIG_DIR"] = spec["gh_config_dir"]
    if spec.get("ssh_config"):
        # root's files show as nobody's in the jail, and ssh refuses a system config it does not trust:
        # the seat's ssh reads only its own config (no identity but [drive] seat_ssh_key, no agent)
        import shlex
        out["GIT_SSH_COMMAND"] = f"ssh -F {shlex.quote(spec['ssh_config'])}"
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
    """Cover one credential path (no symlink followed at the last component: a link is covered where it
    points only when that target is also listed). True when something was covered."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    import stat as s
    if s.S_ISLNK(st.st_mode):
        try:
            st = os.stat(path)
            path = os.path.realpath(path)
        except OSError:
            return False
    if s.S_ISDIR(st.st_mode):
        _mount("tmpfs", path, "tmpfs", MS_RDONLY | MS_NOSUID | MS_NODEV | MS_NOEXEC, "mode=0500,size=16k")
    else:
        _mount(dev_null, path, None, MS_BIND)
    return True


def _bind(src: str, dst: str) -> bool:
    if not (os.path.isdir(src) and os.path.isdir(dst)):
        return False
    _mount(src, dst, None, MS_BIND | MS_REC)
    return True


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


def setup_mounts(spec: dict, uid: int) -> list[str]:
    """In the first namespace (root there): private propagation, a fresh /proc, the seat's .ssh over the
    person's, the runtime dir emptied, then every covered path. The paths covered, in order."""
    _mount(None, "/", None, MS_REC | MS_PRIVATE)
    _mount("proc", "/proc", "proc", MS_NOSUID | MS_NODEV | MS_NOEXEC)
    done = []
    for src, dst in spec.get("binds", ()):
        if _bind(src, dst):
            done.append(dst)
    runtime = spec.get("runtime_dir") or f"/run/user/{uid}"
    if os.path.isdir(runtime):
        _mount("tmpfs", runtime, "tmpfs", MS_NOSUID | MS_NODEV, "mode=0700,size=1m")
        done.append(runtime)
    for path in spec.get("cover", ()):
        if path not in done and _cover(path):
            done.append(path)
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
    r, w = os.pipe()
    init = os.fork()
    if init:                                   # outside the pid namespace: wait for the jail's init
        os.close(w)
        os.close(r)
        return _forward_and_wait(init)
    os.close(r)
    try:                                       # pid 1 of the jail, root of the first user namespace
        setup_mounts(spec, uid)
    except OSError as exc:
        os.write(2, f"seatjail: cannot cover the credential paths ({exc}); not started\n".encode())
        os._exit(EXIT_UNAVAILABLE)
    seat = os.fork()
    if seat:
        os._exit(_forward_and_wait(seat))
    try:                                       # the seat: a nested user namespace locks the mounts
        _unshare(CLONE_NEWUSER | CLONE_NEWNS)
        _map_ids(uid, 0, gid, 0)
        _check(_lib().prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0), "no_new_privs")
        env = seat_env(dict(os.environ), spec)
        os.execvpe(argv[0], argv, env)
    except OSError as exc:
        os.write(2, f"seatjail: cannot start {argv[0]} in the jail ({exc})\n".encode())
        os._exit(EXIT_UNAVAILABLE if "unshare" in str(exc) or "map" in str(exc) else 127)


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
