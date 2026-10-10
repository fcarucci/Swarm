"""`swarm supervise enable|disable`: make the opt-in supervisor choice explicit in the config file
and install or stop the systemd user timer to match."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def set_enabled(config: Path, on: bool) -> None:
    """Write `[supervise] enabled = on` into the config file, keeping every other line."""
    from swarm import codex_config, safefile
    text = config.read_text(encoding="utf-8") if config.exists() else ""
    new = codex_config.set_value(text, "supervise", "enabled", on)
    if config.exists():
        safefile.write_preserving(config, new)
    else:
        config.parent.mkdir(parents=True, exist_ok=True)
        safefile.write_preserving(config, new, mode=0o600)


def stop_timer(run=None) -> str:
    """Disable and stop the timer (if systemctl is here). Returns a one-line result."""
    import shutil
    from swarm.supervisor import systemd
    run = run or systemd.subprocess.run
    if shutil.which("systemctl") is None:
        return "no systemctl here: nothing to stop"
    try:
        res = run(["systemctl", "--user", "disable", "--now", systemd.TIMER],
                  capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"`systemctl --user disable --now {systemd.TIMER}`: {type(exc).__name__}"
    if res.returncode != 0:
        return f"`systemctl --user disable --now {systemd.TIMER}` exited {res.returncode}"
    return f"{systemd.TIMER}: disabled and stopped"


def cmd_switch(cfg: dict, args) -> int:
    from swarm import paths
    from swarm.supervisor import settings as st, systemd
    config = Path(args.config).expanduser()
    on = args.scmd == "enable"
    try:
        set_enabled(config, on)
    except Exception as exc:   # ManualEdit, OSError: say what to set by hand
        print(f"swarm supervise {args.scmd}: can't edit {config} ({exc}); set "
              f"[supervise] enabled = {'true' if on else 'false'} by hand", file=sys.stderr)
        return 1
    print(f"{config}: [supervise] enabled = {'true' if on else 'false'}")
    if not on:
        print(stop_timer())
        return 0
    if paths.IS_WINDOWS:
        print("the supervisor needs a systemd user timer: not available on Windows")
        return 0
    if sys.platform == "darwin":   # no launchd agent is installed: nothing runs passes on a timer
        print("macOS: no timer is installed; the supervisor can't launch replacement sessions on macOS "
              "yet. Stuck agents are still closed during `swarm status`/`watch` sweeps.")
        return 0
    minutes = st.DEFAULTS["timer_minutes"]
    try:
        minutes = st.settings(cfg)["timer_minutes"]
    except st.SettingsError:
        pass
    step = systemd.install(config, minutes, run=systemd.subprocess.run)   # looked up now (tests patch it)
    print(f"{step.name}: {step.status}: {step.detail}")
    return 0 if step.status in ("ok", "changed", "manual", "skipped") else 1
