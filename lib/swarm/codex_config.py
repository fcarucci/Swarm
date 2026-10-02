"""The few ~/.codex/config.toml keys the swarm needs, set without a TOML writer: line edits that
keep every comment and unrelated key, re-parsed (tomllib) to check the result before writing.
Shapes it won't edit safely raise ManualEdit, and the caller prints the step for the user.

Only the keys the swarm changes, with the swarm's own values, are ever reported: config.toml can
hold secrets (MCP env vars, tokens), so no file line, old value or other key is printed."""
from __future__ import annotations

import json
import os
import re
import stat
import tomllib
from pathlib import Path

from swarm import paths, safefile
from swarm import compat

READ_ONLY_STEP = ('Codex runs with sandbox_mode = "read-only" here, so swarm agents can\'t post or reach the '
                  'board. Set sandbox_mode = "workspace-write" in ~/.codex/config.toml (or start swarm '
                  'sessions with `codex -s workspace-write`), then start a new Codex session.')


PROFILE_KEYS = ("sandbox_mode", "sandbox_workspace_write", "agents")
PROFILE_STEP = ("Codex profiles {names} set their own sandbox/agents settings, and a session started with "
                "`-p <name>` (or with `-s`) uses them: the swarm set only the base ~/.codex/config.toml. Add "
                "the writable roots and agents.max_depth = 2 to those profiles by hand, or start swarm "
                "sessions without them; `swarm doctor` run inside the session checks it.")

# Network access is never granted in the base table (it would open every workspace-write
# Codex session of the user, swarm or not). A user who wants swarm agents to reach the board
# directly sets [codex] network_access = true in the swarm config; bootstrap then writes it into
# this profile file, which Codex 0.157.1 layers on the base config for `codex -p swarm` only.
SWARM_PROFILE = "swarm"
PROFILE_HEADER = ("# Written by `swarm bootstrap` because the swarm config sets [codex] network_access = true.\n"
                  "# Only sessions started with `codex -p swarm` get outbound network. The swarm rewrites or\n"
                  "# deletes this file; set [codex] network_access = false in the swarm config to remove it.\n")
PROFILE_BODY = "\n[sandbox_workspace_write]\nnetwork_access = true\n"


def profile_overrides(codex_home: Path, data: dict) -> list[str]:
    """Names of profiles that set sandbox or agents keys of their own: legacy [profiles.<name>]
    tables in config.toml, and $CODEX_HOME/<name>.config.toml files, which Codex 0.157.1's
    `-p/--profile <name>` layers on top of the base config (`codex --help`). Only the names are
    returned; the files' values are never read out."""
    names = {n for n, v in (data.get("profiles") or {}).items()
             if isinstance(v, dict) and any(k in v for k in PROFILE_KEYS)}
    for f in sorted(codex_home.glob("*.config.toml")):
        name = f.name[:-len(".config.toml")]
        if name == SWARM_PROFILE and _ours(f):
            continue                 # the swarm's own opt-in network profile: not a user override
        try:
            d = tomllib.loads(f.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
            names.add(name)          # can't tell: report it
            continue
        if any(k in d for k in PROFILE_KEYS):
            names.add(name)
    return sorted(names)


def effective_sandbox_mode(data: dict) -> str | None:
    """The base config's sandbox mode: the selected profile's (`profile = "<name>"`), else the
    root sandbox_mode, else None (Codex decides per session/project)."""
    prof = data.get("profile")
    if isinstance(prof, str):
        m = ((data.get("profiles") or {}).get(prof) or {}).get("sandbox_mode")
        if m:
            return str(m)
    m = data.get("sandbox_mode")
    return str(m) if m else None


def _describe(table: str, key: str, before, after) -> str:
    """One changed key, with the swarm's own value only (never the user's other values)."""
    if key == "writable_roots":
        added = [r for r in after if r not in (before or [])]
        return f"{table}.{key}: added {', '.join(added)}"
    return f"{table}.{key} = {_literal(after)}"


class ManualEdit(Exception):
    pass


_HEADER = re.compile(r"^\s*\[([^\[\]]+)\]\s*(#.*)?$")


def _literal(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list):
        return "[" + ", ".join(json.dumps(v) for v in value) + "]"
    raise TypeError(value)


def _key_line(line: str, key: str) -> bool:
    return re.match(rf"^\s*{re.escape(key)}\s*=", line) is not None


def _comment(line: str, key: str) -> str:
    """The line's trailing `# ...` comment (with the whitespace before it), "" if none. Raises
    ManualEdit when the value can't be scanned (e.g. an unterminated string)."""
    body = line.rstrip("\n")
    i = body.index("=") + 1
    quote = None
    while i < len(body):
        c = body[i]
        if quote:
            if c == "\\" and quote == '"':
                i += 2
                continue
            if c == quote:
                quote = None
        elif c in "\"'":
            quote = c
        elif c == "#":
            j = i
            while j > 0 and body[j - 1] in " \t":
                j -= 1
            return body[j:]
        i += 1
    if quote:
        raise ManualEdit(f"{key}'s line can't be edited safely")
    return ""


def set_value(text: str, table: str, key: str, value) -> str:
    lines = text.splitlines(keepends=True)
    new = f"{key} = {_literal(value)}\n"
    first_header = next((i for i, l in enumerate(lines) if _HEADER.match(l)), len(lines))
    for i in range(first_header):               # dotted root key, e.g. agents.max_depth = 1
        if _key_line(lines[i], f"{table}.{key}"):
            lines[i] = f"{table}.{key} = {_literal(value)}{_comment(lines[i], f'{table}.{key}')}\n"
            return "".join(lines)
        if _key_line(lines[i], table):         # inline table: sandbox_workspace_write = { ... }
            raise ManualEdit(f"{table} is an inline table")
    start = next((i for i, l in enumerate(lines) if (m := _HEADER.match(l)) and m.group(1).strip() == table), None)
    if start is None:
        sep = "" if not lines or lines[-1].endswith("\n") else "\n"
        return "".join(lines) + sep + f"\n[{table}]\n" + new
    end = next((i for i in range(start + 1, len(lines)) if _HEADER.match(lines[i])), len(lines))
    for i in range(start + 1, end):
        if _key_line(lines[i], key):
            if lines[i].count("[") != lines[i].count("]"):
                raise ManualEdit(f"{table}.{key} spans several lines")
            lines[i] = f"{key} = {_literal(value)}{_comment(lines[i], f'{table}.{key}')}\n"
            return "".join(lines)
    lines.insert(start + 1, new)
    return "".join(lines)


def _absolute(p) -> Path:
    """A configured dir as the swarm opens it: ~ expanded, then relative to the current
    directory (like every other use of spool_dir/marker_dir), made absolute without resolving
    symlinks. Codex's writable_roots must be absolute."""
    return Path(os.path.abspath(Path(str(p)).expanduser()))


def _ensure_private_root(d: Path) -> str | None:
    """Make sure `d` is a private (0700), user-owned, plain directory that a Codex sandbox may be
    granted (like spool.py's spool directory: the same contract, kept for every granted root,
    since nothing else should read or write where the sandbox can). A missing root is created and
    then always forced to exactly 0700: mkdir's mode is masked by the umask, so a restrictive one
    (e.g. 0o277) could otherwise leave a brand-new root without owner write -- a directory this
    call just created is always safe to force to what was asked for. An existing root's group/
    other bits are tightened away, but if the owner itself lacks rwx (e.g. 0500) that is left
    alone and refused instead: permissions are only ever narrowed, never widened, for a directory
    the swarm didn't create. Returns None on success, else the reason it isn't usable (never
    raises)."""
    if d.is_symlink():
        return "it is a symlink, not a plain directory"
    created = False
    if not d.exists():
        # every missing component 0700, whatever the umask (mkdir(parents=True) makes the
        # parents 0777 & ~umask: 0775 under umask 002, which safefs then refuses)
        missing = []
        p = d
        while not os.path.lexists(p) and p != p.parent:
            missing.append(p)
            p = p.parent
        try:
            for p in reversed(missing):
                try:
                    p.mkdir(mode=0o700)
                except FileExistsError:
                    continue
                fd = compat.open(p, os.O_RDONLY | compat.O_DIRECTORY | compat.O_NOFOLLOW | compat.O_CLOEXEC)
                try:
                    compat.fchmod(fd, 0o700)
                finally:
                    os.close(fd)
        except OSError as exc:
            return f"can't create it ({exc.strerror or exc})"
        created = True
    elif not d.is_dir():
        return "it exists and is not a directory"
    try:
        st = d.stat()
    except OSError as exc:
        return f"can't stat it ({exc.strerror or exc})"
    if st.st_uid != compat.uid():
        return f"it belongs to another user (uid {st.st_uid})"
    if not compat.HAS_MODES:   # Windows: no mode bits; the profile's ACLs apply
        return None
    mode = stat.S_IMODE(st.st_mode)
    if created:
        if mode != 0o700:   # the umask masked mkdir's own mode: force it to what we asked for
            try:
                d.chmod(0o700)
            except OSError as exc:
                return f"can't set its permissions to 0700 ({exc.strerror or exc})"
        return None
    if mode & 0o700 != 0o700:
        return f"it exists with mode {oct(mode)}: the owner needs rwx (0700), which the swarm won't add"
    if mode & 0o077:
        try:
            d.chmod(0o700)
        except OSError as exc:
            return f"can't tighten its permissions to 0700 ({exc.strerror or exc})"
    return None


def _ours(f: Path) -> bool:
    """Whether a profile file is the one the swarm wrote (it starts with PROFILE_HEADER)."""
    try:
        with open(f, "rb") as fh:
            return fh.read(len(PROFILE_HEADER.encode())) == PROFILE_HEADER.encode()
    except OSError:
        return False


def spool_path(cfg: dict) -> Path:
    """The configured spool dir, absolute ({uid} expanded, as the config loader does)."""
    return _absolute(str(cfg["board"]["spool_dir"]).replace("{uid}", str(compat.uid())))


def board_dir(cfg: dict) -> Path | None:
    """The directory a local (sqlite/file) board writes in, or None for a server board."""
    from swarm.board import board_backend   # lazy: the hooks import this module and never the board
    backend = board_backend(cfg)
    if backend == "sqlite":
        return _absolute(cfg["sqlite"]["path"]).parent      # the database, its journal and WAL files
    if backend == "file":
        return _absolute(cfg["file"]["path"])
    return None


def _opt(cfg: dict, key: str) -> bool:
    return (cfg.get("codex") or {}).get(key) is True


def required(cfg: dict) -> dict:
    """What the swarm needs in the base Codex config: the spool and marker dirs as writable
    roots, each named explicitly, and never the state dir itself (the host-trusted files next to
    them) nor network access. The board dir only when the user opts in ([codex] board_writable,
    which doctor reports as a FAIL: sandboxed agents can then forge and tamper with board rows)."""
    roots = []
    wanted = [spool_path(cfg), _absolute(cfg["hook"]["marker_dir"])]
    if _opt(cfg, "board_writable") and board_dir(cfg) is not None:
        wanted.append(board_dir(cfg))
    for p in wanted:
        if str(p) not in roots:
            roots.append(str(p))
    return {("sandbox_workspace_write", "writable_roots"): roots,
            ("agents", "max_depth"): 2}


def _manual_keys(cfg: dict) -> str:
    return (f"[sandbox_workspace_write] writable_roots += "
            f"{required(cfg)[('sandbox_workspace_write', 'writable_roots')]}; [agents] max_depth = 2")


def _current(data: dict, table: str, key: str):
    return (data.get(table) or {}).get(key)


def _unsupported_shape(data: dict) -> str | None:
    """Why the swarm's keys can't be edited in this (valid) config, or None. Names the key and
    the expected type only, never the value."""
    for table in ("sandbox_workspace_write", "agents"):
        if table in data and not isinstance(data[table], dict):
            return f"{table} is not a table"
    sw, ag = data.get("sandbox_workspace_write") or {}, data.get("agents") or {}
    roots = sw.get("writable_roots")
    if roots is not None and not (isinstance(roots, list) and all(isinstance(r, str) for r in roots)):
        return "sandbox_workspace_write.writable_roots is not a list of strings"
    if "network_access" in sw and not isinstance(sw["network_access"], bool):
        return "sandbox_workspace_write.network_access is not true/false"
    depth = ag.get("max_depth")
    if depth is not None and (isinstance(depth, bool) or not isinstance(depth, int)):
        return "agents.max_depth is not an integer"
    return None


def _position(exc: Exception) -> str:
    m = re.search(r"\(at line (\d+), column (\d+)\)", str(exc))
    return f"invalid TOML at line {m.group(1)} column {m.group(2)}" if m else "invalid TOML"


def apply(path: Path, cfg: dict) -> tuple[str, str]:
    """Set the swarm's keys in the base Codex config. ("ok" | "changed" | "manual", detail); the
    detail names only what the swarm changed or what the user must do."""
    try:
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        data = tomllib.loads(text)
    except (OSError, UnicodeDecodeError) as exc:
        return "manual", f"can't read {path} ({getattr(exc, 'strerror', None) or type(exc).__name__}); fix it, then run `swarm bootstrap --host codex`"
    except tomllib.TOMLDecodeError as exc:
        return "manual", f"{path} doesn't parse ({_position(exc)}); fix it, then run `swarm bootstrap --host codex`"
    bad = _unsupported_shape(data)
    if bad:
        return "manual", f"{path}: {bad}, which the swarm won't edit. Set by hand: {_manual_keys(cfg)}"
    # Codex's sandbox (bubblewrap) bind-mounts each writable root at session start; a root that
    # doesn't exist yet on disk makes the mount (and so every command in the sandbox) fail
    # immediately, before the swarm ever gets to create it lazily on first use. Ensure every root
    # this call grants exists now (private, 0700, owned by this user -- the spool contract, kept
    # for every root for the same reason: nothing else should be able to read or write into a
    # directory Codex's sandbox is told it may write), regardless of whether the config text needs
    # changing below, and refuse before writing config.toml if any root can't be made usable (a
    # granted-but-broken root is worse than not granting it: the sandbox would still fail, but
    # silently, with no record of why).
    for root in required(cfg)[("sandbox_workspace_write", "writable_roots")]:
        problem = _ensure_private_root(Path(root))
        if problem:
            return "manual", (f"{path}: can't grant the Codex sandbox {root} ({problem}); fix it by hand, "
                              f"then run `swarm bootstrap --host codex`")
    try:
        text, removed, grant_notes = _without_old_grants(path, text, data)
        data = tomllib.loads(text)
    except ManualEdit as exc:
        return "manual", f"{path}: {exc}. Remove by hand: {old_grants_step(path)}"
    new = text
    wanted = {}
    try:
        for (table, key), value in required(cfg).items():
            have = _current(data, table, key)
            if key == "writable_roots":
                have = list(have or [])
                value = have + [r for r in value if r not in have]
            if key == "max_depth" and isinstance(have, int) and have >= value:
                continue
            if have == value:
                continue
            new = set_value(new, table, key, value)
            wanted[(table, key)] = value
    except ManualEdit as exc:
        return "manual", f"{path}: {exc}. Set by hand: {_manual_keys(cfg)}"
    read_only = effective_sandbox_mode(data) == "read-only"
    profile_status, profile_detail = _apply_profile(path.parent, cfg)
    profiles = profile_overrides(path.parent, data)
    notes = (grant_notes + ([READ_ONLY_STEP] if read_only else [])
             + ([PROFILE_STEP.format(names=", ".join(profiles))] if profiles else [])
             + ([profile_detail] if profile_status == "manual" else []))
    extra = [profile_detail] if profile_status == "changed" else []
    if not wanted and not removed:
        if notes:
            return "manual", f"{path}: " + "\n".join(extra + notes)
        if extra:
            return "changed", "\n".join(extra)
        return "ok", f"{path} already has the swarm's settings"
    try:
        check = tomllib.loads(new)
    except tomllib.TOMLDecodeError:
        check = {}
    if any(_current(check, t, k) != v for (t, k), v in wanted.items()):
        return "manual", f"{path}: the edit didn't re-parse as intended; left unchanged"
    saved = safefile.backup(path)
    safefile.write_preserving(path, new)
    changes = [_describe(t, k, _current(data, t, k), v) for (t, k), v in wanted.items()]
    detail = f"{path}: " + "; ".join((["set " + "; ".join(changes)] if changes else []) + removed) \
        + (f" (backup: {saved.name})" if saved else "")
    detail = "\n".join([detail] + extra)
    return ("manual", detail + "\n" + "\n".join(notes)) if notes else ("changed", detail)


def _apply_profile(codex_home: Path, cfg: dict) -> tuple[str, str]:
    """The opt-in network profile ($CODEX_HOME/swarm.config.toml): written when [codex]
    network_access = true, removed when not. A file there the swarm didn't write is never touched."""
    f = codex_home / f"{SWARM_PROFILE}.config.toml"
    want = _opt(cfg, "network_access")
    exists = os.path.lexists(f)
    if exists and (f.is_symlink() or not f.is_file() or not _ours(f)):
        if want:
            return "manual", (f"{f} exists and wasn't written by the swarm: left alone. For direct board access, "
                              f"add [sandbox_workspace_write] network_access = true to it by hand")
        return "ok", ""
    if not want:
        if exists:
            f.unlink()
            return "changed", f"removed {f} ([codex] network_access is off)"
        return "ok", ""
    if exists and f.read_text(encoding="utf-8") == PROFILE_HEADER + PROFILE_BODY:
        return "ok", ""
    safefile.write_preserving(f, PROFILE_HEADER + PROFILE_BODY, mode=0o600)
    return "changed", (f"wrote {f}: sessions started with `codex -p swarm` get network access "
                       f"([codex] network_access = true); others keep their sandbox")


# ----------------------------------------------------------------- taking back 0.1.0-pre grants

def _backups(path: Path) -> list[Path]:
    return sorted(path.parent.glob(f"{path.name}.pre-swarm-*"))


def _swarm_set_network(path: Path) -> bool | None:
    """Whether network_access = true in the base table was the swarm's doing: the backup the
    swarm took before its first edit (the oldest) shows it wasn't there. None: no backup, so
    can't tell."""
    backups = _backups(path)
    if not backups:
        return None
    try:
        before = tomllib.loads(backups[0].read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
    sw = before.get("sandbox_workspace_write")
    return not (isinstance(sw, dict) and sw.get("network_access") is True)


def old_grants_step(path: Path) -> str:
    return (f"in {path} [sandbox_workspace_write], take {paths.state_dir()} out of writable_roots and "
            f"delete network_access = true unless you set it yourself")


def _without_old_grants(path: Path, text: str, data: dict) -> tuple[str, list[str], list[str]]:
    """The config text without what swarm 0.1.0-pre granted: the whole state dir as a writable
    root and, only if the swarm's own backup shows the swarm set it, network_access = true in
    the base table. Runs once: the state dir root is the sign of an old grant, so a user who
    sets network_access later isn't overruled. (text, removed descriptions, notes for the user).
    Raises ManualEdit when the lines can't be edited safely."""
    from swarm.bootstrap import OLD_SPOOL
    sw = data.get("sandbox_workspace_write") if isinstance(data.get("sandbox_workspace_write"), dict) else {}
    roots = sw.get("writable_roots") if isinstance(sw.get("writable_roots"), list) else []
    state = str(_absolute(paths.state_dir()))
    shared = str(_absolute(OLD_SPOOL))   # the pre-0.1.0 shared spool
    old = [r for r in roots if isinstance(r, str) and str(_absolute(r)) in (state, shared)]
    if not old:
        return text, [], []
    removed, notes = [], []
    text = set_value(text, "sandbox_workspace_write", "writable_roots", [r for r in roots if r not in old])
    for r in dict.fromkeys(str(_absolute(r)) for r in old):
        removed.append(f"removed {r} from sandbox_workspace_write.writable_roots (granted by an earlier swarm)")
    if sw.get("network_access") is True and any(str(_absolute(r)) == state for r in old):
        by_swarm = _swarm_set_network(path)
        if by_swarm:
            text = remove_key(text, "sandbox_workspace_write", "network_access")
            removed.append("removed network_access = true from [sandbox_workspace_write] (set by an earlier swarm)")
        elif by_swarm is None:
            notes.append(f"{path}: [sandbox_workspace_write] network_access = true gives every workspace-write "
                         f"Codex session outbound network; an earlier swarm set it, unless you did. Delete it "
                         f"if you didn't (the swarm no longer needs it)")
    return text, removed, notes


def remove_old_grants(path: Path) -> tuple[str, str]:
    """For `swarm migrate`: only the 0.1.0-pre grants taken back (see _without_old_grants).
    ("ok" | "changed" | "manual", detail)."""
    try:
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        data = tomllib.loads(text)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return "manual", f"can't read {path}: {old_grants_step(path)}"
    if _unsupported_shape(data):
        return "manual", f"{path}: {old_grants_step(path)}"
    try:
        new, removed, notes = _without_old_grants(path, text, data)
    except ManualEdit as exc:
        return "manual", f"{path}: {exc}. {old_grants_step(path)}"
    if not removed:
        return ("manual", "\n".join(notes)) if notes else ("ok", f"{path}: no old swarm grants")
    saved = safefile.backup(path)
    safefile.write_preserving(path, new)
    detail = f"{path}: " + "; ".join(removed) + (f" (backup: {saved.name})" if saved else "")
    return ("manual", "\n".join([detail] + notes)) if notes else ("changed", detail)


def remove_key(text: str, table: str, key: str) -> str:
    """The text without `key = ...` in [table] (or a dotted root `table.key = ...` line). Raises
    ManualEdit when the key's line isn't a single line the swarm can find."""
    lines = text.splitlines(keepends=True)
    first_header = next((i for i, l in enumerate(lines) if _HEADER.match(l)), len(lines))
    for i in range(first_header):
        if _key_line(lines[i], f"{table}.{key}"):
            _comment(lines[i], f"{table}.{key}")
            del lines[i]
            return "".join(lines)
    start = next((i for i, l in enumerate(lines) if (m := _HEADER.match(l)) and m.group(1).strip() == table), None)
    if start is None:
        raise ManualEdit(f"{table}.{key} is not where the swarm can edit it")
    end = next((i for i in range(start + 1, len(lines)) if _HEADER.match(lines[i])), len(lines))
    for i in range(start + 1, end):
        if _key_line(lines[i], key):
            _comment(lines[i], f"{table}.{key}")
            del lines[i]
            return "".join(lines)
    raise ManualEdit(f"{table}.{key} is not where the swarm can edit it")
