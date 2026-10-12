"""A drive seat's credential-free environment (Task 20; spec 4.0: the shell guard is a guardrail, this
is the boundary). Every drive seat launch (Claude and Codex, every role) goes through `wrap`:

- the seat's home dir, <supervisor private dir>/seat-home/<job>/<seat> (0700; privfs), holds what the seat needs
  and nothing that pushes: .ssh/known_hosts (a copy of the person's) and .ssh/config (no identity, no
  agent, batch mode), gitconfig (an identity, no credential helper, GitHub ssh URLs fetched over
  anonymous HTTPS) and an empty gh config dir;
- the seat runs in swarm.seatjail: that .ssh is mounted over the person's ~/.ssh, and every path of
  `cover_paths` (the person's gh config, git credential stores, ~/.netrc, other secret dirs, and the
  pinned credentials of this host's drive jobs) is covered, so a seat cannot read them by any path;
- the harness keeps what it runs with: ~/.claude, ~/.claude.json and ~/.codex are not covered (Claude
  Code's and Codex's own login; neither holds a push credential).

HOME is not changed: ssh resolves ~ from the password database, not $HOME, and any process can open
an absolute path, so a different $HOME alone hides nothing; the mounts do. The supervisor's land path
(driver_land, mirror) never runs in a jail and keeps its pinned credentials.

Fetch for seats: GitHub remotes are read over anonymous HTTPS (a public repository). A private one
needs a READ-ONLY deploy key per destination, placed outside every covered path (e.g.
~/.local/state/swarm/seat-keys/<name>, 0600) and named in [drive] seat_ssh_key; it is the only identity
the seat's ssh config names."""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from swarm import paths

# Paths below the person's home that hold a push credential (or other secrets) and that a seat never
# reads. ~/.ssh is not here: the seat's own .ssh is mounted over it.
HOME_COVER = (".config/gh", ".config/hub", ".git-credentials", ".config/git/credentials", ".netrc",
              ".gitconfig", ".config/git/config", ".gnupg", ".secrets", ".docker/config.json",
              ".config/glab-cli", ".config/tea", ".pgpass", ".aws", ".config/gcloud", ".kube")
# The same credentials where macOS and Windows keep them (Task 21 ports the jail itself; these lists are
# what a platform jail must cover). The macOS Keychain (gh's token by default) and the Windows Credential
# Manager / ssh-agent service are not files: a platform jail must deny those services, not paths.
MACOS_COVER = ("Library/Application Support/gh", "Library/Application Support/GitHub CLI",
               "Library/Keychains")
WINDOWS_COVER = (".ssh", "AppData/Roaming/GitHub CLI", ".git-credentials", ".gitconfig",
                 "AppData/Local/GitCredentialManager", "AppData/Roaming/GitCredentialManager", "_netrc")
SEAT_HOME = "seat-home"
_SLUG = re.compile(r"[^A-Za-z0-9._-]+")
ISOLATION_MODES = ("required", "off")


class SeatIsolationError(RuntimeError):
    """A drive seat that cannot be launched without the person's credentials: never started."""


def _drive(cfg: dict) -> dict:
    d = (cfg or {}).get("drive")
    return d if isinstance(d, dict) else {}


def mode(cfg: dict) -> str:
    """[drive] seat_isolation: "required" (the default: a seat that cannot be jailed is not started) or
    "off" (the person's explicit opt-out: seats run with the person's credentials, doctor says so)."""
    value = str(_drive(cfg).get("seat_isolation", "required")).strip().lower()
    if value not in ISOLATION_MODES:
        raise SeatIsolationError(f"[drive] seat_isolation must be one of {', '.join(ISOLATION_MODES)}, "
                                 f"not {value!r}")
    return value


def _slug(name: str) -> str:
    import hashlib
    s = _SLUG.sub("_", str(name)).strip("._") or "x"
    return f"{s[:60]}-{hashlib.sha256(str(name).encode()).hexdigest()[:8]}"


def home_dir(job: str, seat: str) -> Path:
    from swarm.supervisor.settings import private_dir
    return private_dir() / SEAT_HOME / _slug(job) / _slug(seat)


def _git_identity(cfg: dict) -> tuple[str | None, str | None]:
    """[drive] seat_git_name / seat_git_email, else the person's global git identity."""
    d = _drive(cfg)
    name, email = d.get("seat_git_name"), d.get("seat_git_email")
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
              # GitHub ssh remotes are fetched anonymously over HTTPS; a push there has no credential
              '[url "https://github.com/"]', "\tinsteadOf = git@github.com:",
              "\tinsteadOf = ssh://git@github.com/"]
    return "\n".join(lines) + "\n"


def ssh_config_text(cfg: dict) -> str:
    key = _drive(cfg).get("seat_ssh_key")
    lines = ["# written by the swarm supervisor: a drive seat's ssh names no push identity",
             "Host *", "\tIdentityAgent none", "\tIdentitiesOnly yes", "\tBatchMode yes",
             "\tForwardAgent no", "\tControlMaster no", "\tControlPath none"]
    if key:
        if not os.path.isabs(str(key)):
            raise SeatIsolationError("[drive] seat_ssh_key must be an absolute path (a read-only deploy key)")
        lines.append(f'\tIdentityFile "{_clean(str(key))}"')
    else:
        lines.append("\tIdentityFile /dev/null")
    return "\n".join(lines) + "\n"


def prepare(cfg: dict, job: str, seat: str) -> Path:
    """Create or refresh the seat's home dir in the supervisor's private directory (privfs: every
    component opened without following a link, this user's, 0700; files replaced, never followed); its path."""
    from swarm import safefs
    from swarm.supervisor import privfs
    from swarm.supervisor.settings import PrivateDirError
    try:
        text = (paths.home() / ".ssh" / "known_hosts").read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    fds = []
    try:
        fds.append(privfs.open_dir(SEAT_HOME))
        for name in (_slug(job), _slug(seat)):
            fds.append(safefs.open_sub(fds[-1], name, strict_mode=0o700))
        h = fds[-1]
        privfs.write_atomic(h, "gitconfig", gitconfig_text(cfg))
        for sub in (".ssh", "gh"):
            os.close(safefs.open_sub(h, sub, strict_mode=0o700))
        ssh = safefs.open_sub(h, ".ssh", strict_mode=0o700)
        try:
            privfs.write_atomic(ssh, "known_hosts", text)
            privfs.write_atomic(ssh, "config", ssh_config_text(cfg))
        finally:
            os.close(ssh)
    except (safefs.UnsafePathError, PrivateDirError, OSError) as exc:
        raise SeatIsolationError(f"the seat's home dir cannot be prepared safely: {exc}") from exc
    finally:
        for fd in fds:
            os.close(fd)
    return home_dir(job, seat)


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
    """The credential paths pinned by every drive job with an owner record on this host."""
    from swarm import enrolment
    from swarm.board.autoinit import store_key
    from swarm.supervisor import ledger
    out: list[str] = []
    try:
        bk = store_key(cfg)
        jobs = [x for x in ledger.unfinished(bk) if isinstance(x, ledger.Ledger)]
    except Exception:
        return out
    for led in jobs:
        try:
            rec = enrolment.find_job_owner(bk, led.job)
        except Exception:
            continue
        if rec is not None and rec.owner:
            out += _pin_paths(rec.owner)
    return out


def cover_paths(cfg: dict, home: Path | None = None) -> list[str]:
    """Every path covered in a seat's jail: HOME_COVER, [drive] seat_cover_paths, the jobs' pins, any
    ssh-agent socket directories in the temp dir, and the person's gh config dir if moved elsewhere."""
    home = Path(home or paths.home())
    out = [str(home / p) for p in platform_cover()]
    extra = _drive(cfg).get("seat_cover_paths") or []
    if isinstance(extra, str):
        extra = [extra]
    out += [os.path.expanduser(str(p)) for p in extra]
    out += pinned_paths(cfg)
    for var in ("GH_CONFIG_DIR", "SSH_AUTH_SOCK"):
        v = os.environ.get(var)
        if v and os.path.isabs(v):
            out.append(v)
    tmp = "/tmp"
    try:
        out += [os.path.join(tmp, n) for n in os.listdir(tmp) if n.startswith("ssh-")]
    except OSError:
        pass
    seen, uniq = set(), []
    for p in out:
        p = os.path.normpath(p)
        if p not in seen and os.path.isabs(p):
            seen.add(p)
            uniq.append(p)
    return uniq


def platform_cover(platform: str | None = None) -> tuple:
    """The home-relative paths covered on `platform` (sys.platform)."""
    platform = platform or sys.platform
    if platform == "darwin":
        return HOME_COVER + MACOS_COVER
    if platform.startswith("win"):
        return HOME_COVER + WINDOWS_COVER
    return HOME_COVER


def jail_spec(cfg: dict, seat_home: Path, home: Path | None = None) -> dict:
    home = Path(home or paths.home())
    return {"binds": [[str(seat_home / ".ssh"), str(home / ".ssh")]],
            "cover": cover_paths(cfg, home),
            "gitconfig": str(seat_home / "gitconfig"),
            "gh_config_dir": str(seat_home / "gh"),
            "ssh_config": str(home / ".ssh" / "config"),   # the seat's own, mounted over ~/.ssh
            "runtime_dir": f"/run/user/{os.getuid()}" if hasattr(os, "getuid") else None}


def jail_script() -> str:
    return str(paths.LIB_DIR / "swarm" / "seatjail.py")


def wrap(cfg: dict, argv: list, *, job: str, seat: str, python: str | None = None,
         probe=None) -> list:
    """The drive seat's argv, jailed: [python, -I, seatjail.py, SPEC, --, *argv]. Unchanged when
    [drive] seat_isolation = "off". SeatIsolationError when this host cannot jail a seat (`probe()`,
    default seatjail.available, says why): the seat is never started with the person's credentials."""
    if mode(cfg) == "off":
        return list(argv)
    from swarm import seatjail
    ok, why = (probe or seatjail.available)()
    if not ok:
        raise SeatIsolationError(f"this host cannot isolate a drive seat from the person's credentials ({why}); "
                                 "not started. Enable unprivileged user namespaces, or set [drive] "
                                 "seat_isolation = \"off\" to accept seats that can read them")
    h = prepare(cfg, job, seat)
    spec = jail_spec(cfg, h)
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
[ -n "$SSH_AUTH_SOCK$GH_TOKEN$GITHUB_TOKEN" ] && { echo "ENV credential variable set"; fail=1; }
h=$(git config --get-all credential.helper 2>/dev/null | tr -d '[:space:]')
[ -n "$h" ] && { echo "HELPER git credential helper configured"; fail=1; }
if command -v gh >/dev/null 2>&1; then gh auth status >/dev/null 2>&1 && { echo "GH gh is logged in"; fail=1; }; fi
if [ "${SWARM_PROBE_NETWORK:-0}" = 1 ] && command -v ssh >/dev/null 2>&1; then
  out=$(timeout 20 ssh -F "$HOME/.ssh/config" -T -o ConnectTimeout=10 git@github.com 2>&1)
  case "$out" in *"successfully authenticated"*) echo "SSH github accepts a key"; fail=1;; esac
fi
[ $fail = 0 ] && echo SEAT-CLEAN
exit $fail
"""


def probe(cfg: dict, *, network: bool = False, run=None, available=None) -> tuple[bool | None, str]:
    """Run PROBE in a seat's jail. (True, detail) when no credential is reachable; (False, what is
    reachable); (None, why) when isolation is off or this host cannot jail a seat."""
    import subprocess
    if mode(cfg) == "off":
        return None, "off ([drive] seat_isolation = \"off\"): drive seats can read the person's credentials"
    from swarm import seatjail
    ok, why = (available or seatjail.available)()
    if not ok:
        return None, f"unavailable: {why}"
    argv = wrap(cfg, ["sh", "-c", PROBE, "probe", *cover_paths(cfg)], job="_doctor", seat="probe",
                probe=lambda: (True, ""))
    env = seatjail.scrub_env(dict(os.environ))
    env["SWARM_PROBE_NETWORK"] = "1" if network else "0"
    try:
        res = (run or subprocess.run)(argv, capture_output=True, text=True, timeout=60, env=env,
                                      cwd=paths.state_dir())
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"probe failed to run ({type(exc).__name__})"
    lines = [x for x in (res.stdout or "").splitlines() if x.strip()]
    if res.returncode == 0 and "SEAT-CLEAN" in lines:
        return True, "active: no key, token, agent or credential helper reachable from a seat"
    return False, "; ".join(lines[:6]) or f"probe exit {res.returncode}"
