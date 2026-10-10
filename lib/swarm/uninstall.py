"""`swarm uninstall [--purge] [--dry-run]`: remove what Swarm set up outside the plugin.

Removed: the supervisor's systemd user units (stopped first), the ~/.local/bin launcher when it is
the one Swarm wrote, ~/.local/share/swarm (venv, bytecode cache, host-only files), the spool path
Swarm added to Claude's sandbox.filesystem.allowWrite, and the writable roots it added to Codex's
config.toml (each file backed up first). With --purge also ~/.local/state/swarm (local file
boards, markers, spool), the default SQLite board directory and the config file. The plugin itself
is removed with the host's own command (`/plugin uninstall`, Codex's plugin commands)."""
from __future__ import annotations

import json
import shutil
import sys
import tomllib
from pathlib import Path


def _plan(cfg: dict, config: Path, purge: bool) -> list[tuple[str, str, object]]:
    """[(kind, description, target)]: what an uninstall would do, in order."""
    from swarm import bootstrap, cli, codex_config, paths
    from swarm.supervisor import systemd
    out: list[tuple[str, str, object]] = []
    units = systemd.unit_dir()
    if any((units / n).exists() for n in (systemd.TIMER, systemd.SERVICE)):
        out.append(("timer", f"stop and disable {systemd.TIMER}", None))
        for n in (systemd.TIMER, systemd.SERVICE):
            if (units / n).exists():
                out.append(("file", f"remove {units / n}", units / n))
    launcher = paths.launcher_path()
    if launcher.exists() and bootstrap.launcher_target(launcher) is not None:
        out.append(("file", f"remove {launcher}", launcher))
    if paths.share_dir().exists():
        out.append(("tree", f"remove {paths.share_dir()}", paths.share_dir()))
    spool = str(codex_config.spool_path(cfg))
    settings = cli.claude_settings_path()
    if spool in _allow_write(settings):
        out.append(("claude", f"remove {spool} from sandbox.filesystem.allowWrite in {settings}", (settings, spool)))
    codex = bootstrap._codex_home() / "config.toml"
    ours = set(codex_config.required(cfg)[("sandbox_workspace_write", "writable_roots")])
    if ours & set(_writable_roots(codex)):
        out.append(("codex", f"remove {sorted(ours & set(_writable_roots(codex)))} from "
                              f"[sandbox_workspace_write] writable_roots in {codex}", (codex, ours)))
    if purge:
        if paths.state_dir().exists():
            out.append(("tree", f"remove {paths.state_dir()}", paths.state_dir()))
        board = Path(str((cfg.get("sqlite") or {}).get("path") or "")).expanduser()
        default_board_dir = paths.home() / ".local" / "share" / "swarm-board"
        if board.parent == default_board_dir and default_board_dir.exists():
            out.append(("tree", f"remove {default_board_dir}", default_board_dir))
        if config.exists():
            if _under(config, paths.home() / ".config" / "swarm"):
                out.append(("file", f"remove {config}", config))
            else:
                out.append(("keep", f"kept {config} (not Swarm's default location)", None))
    return out


def _under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def _manual_timer_steps() -> str:
    from swarm.supervisor import systemd
    d = systemd.unit_dir()
    return (f"  systemctl --user disable --now {systemd.TIMER}\n"
            f"  rm -f {d / systemd.SERVICE} {d / systemd.TIMER}\n"
            "  systemctl --user daemon-reload\n"
            "then run `swarm uninstall` again")


def _allow_write(settings: Path) -> list:
    try:
        data = json.loads(settings.read_text()) if settings.exists() else {}
        aw = data.get("sandbox", {}).get("filesystem", {}).get("allowWrite", [])
        return aw if isinstance(aw, list) else []
    except (OSError, ValueError, AttributeError):
        return []


def _writable_roots(codex: Path) -> list:
    try:
        data = tomllib.loads(codex.read_text(encoding="utf-8")) if codex.exists() else {}
        roots = (data.get("sandbox_workspace_write") or {}).get("writable_roots", [])
        return roots if isinstance(roots, list) else []
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, AttributeError):
        return []


def _do(kind: str, target) -> str | None:
    """Carry out one step. Returns an error text, or None."""
    from swarm import codex_config, safefile
    from swarm.supervisor import switch, systemd
    try:
        if kind == "timer":
            msg = switch.stop_timer()
            return None if "disabled" in msg or "nothing to stop" in msg else msg
        if kind == "file":
            target.unlink(missing_ok=True)
            if target.parent == systemd.unit_dir() and shutil.which("systemctl"):
                systemd.subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True,
                                       text=True, timeout=20)
        elif kind == "tree":
            shutil.rmtree(target)
        elif kind == "claude":
            path, spool = target
            data = json.loads(path.read_text())
            fs = data["sandbox"]["filesystem"]
            fs["allowWrite"] = [p for p in fs["allowWrite"] if p != spool]
            safefile.backup(path)
            safefile.write_preserving(path, json.dumps(data, indent=2) + "\n")
        elif kind == "codex":
            path, ours = target
            text = path.read_text(encoding="utf-8")
            keep = [r for r in _writable_roots(path) if r not in ours]
            safefile.backup(path)
            new = (codex_config.set_value(text, "sandbox_workspace_write", "writable_roots", keep) if keep
                   else codex_config.remove_key(text, "sandbox_workspace_write", "writable_roots"))
            safefile.write_preserving(path, new)
    except Exception as exc:   # report and go on: an uninstall removes what it can
        return f"{type(exc).__name__}: {exc}"
    return None


def cmd_uninstall(cfg: dict, args) -> int:
    config = Path(args.config).expanduser()
    plan = _plan(cfg, config, args.purge)
    if not plan:
        print("nothing of Swarm's to remove outside the plugin")
    failed = 0
    for kind, text, target in plan:
        if kind == "keep":
            print(text)
            continue
        if args.dry_run:
            print(f"would {text}")
            continue
        err = _do(kind, target)
        print(f"{text}: {'failed: ' + err if err else 'done'}")
        if err and kind == "timer":
            # a timer that may still be loaded must keep its unit files and the venv it runs:
            # stop here, change nothing else, and say what to run (e.g. from a login shell)
            print("stopped: nothing else was removed. Stop the timer from a shell with a user "
                  "systemd session, then:\n" + _manual_timer_steps(), file=sys.stderr)
            return 1
        failed += bool(err)
    if not args.dry_run:
        print("Now remove the plugin itself: `/plugin uninstall swarm@swarm` in Claude Code, or with "
              "Codex's plugin commands.")
    return 1 if failed else 0
