"""Private Codex tool permissions: normal role sandbox plus its typed broker only.

Never edit the owner's config. Copy its semantic values into a private, read-only
launch file and replace legacy sandbox policy with a named permission profile.
"""
from __future__ import annotations

import datetime
import json
import math
import os
import tomllib

PROFILE = "swarm-launch-broker"


def _literal(value) -> str:
    """Encode the TOML value types accepted by tomllib, including nested tables."""
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value):
            return "nan"
        return repr(value) if math.isfinite(value) else "inf" if value > 0 else "-inf"
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, list):
        return "[" + ", ".join(_literal(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(_literal(k) + " = " + _literal(v) for k, v in value.items()) + "}"
    raise ValueError("unsupported Codex config value")


def codex_config(raw: bytes, broker: str, *, read_only: bool) -> str:
    """Keep user values, replacing only sandbox selection for the private launch.

    Legacy sandbox_mode overrides named permissions, including in legacy profiles.
    Remove those selectors and their workspace policy; the launch's filesystem
    jail remains the authority over source, credentials and private host files.
    """
    data = tomllib.loads(raw.decode("utf-8"))
    permissions = data.setdefault("permissions", {})
    if not isinstance(permissions, dict) or PROFILE in permissions:
        raise ValueError("reserved Codex launch permission profile already exists")
    data.pop("sandbox_mode", None)
    data.pop("sandbox_workspace_write", None)
    profiles = data.get("profiles") or {}
    if not isinstance(profiles, dict):
        raise ValueError("Codex config profiles must be tables")
    for profile in profiles.values():
        if isinstance(profile, dict):
            profile.pop("sandbox_mode", None)
            profile.pop("sandbox_workspace_write", None)
    data["default_permissions"] = PROFILE
    permissions[PROFILE] = {"extends": ":read-only" if read_only else ":workspace",
                            "filesystem": {os.path.dirname(os.path.dirname(broker)): "read", broker: "write"},
                            "network": {"enabled": False}}
    rendered = "".join(_literal(k) + " = " + _literal(v) + "\n" for k, v in data.items())
    # Refuse rather than silently changing semantics if a new TOML type appears.
    if _literal(tomllib.loads(rendered)) != _literal(data):
        raise ValueError("Codex launch config could not be copied faithfully")
    return rendered


def is_codex_exec(argv: list) -> bool:
    """Recognize the trusted native Codex exec contract, including wrappers."""
    return "exec" in argv and "--skip-git-repo-check" in argv


def codex_argv(argv: list) -> list:
    """Select the private policy only after a real jail has been built.

    Match the Codex exec contract, also behind a configured wrapper. Best-effort
    and isolation-off launches never call this and keep their legacy policy.
    """
    if not is_codex_exec(argv):
        return list(argv)
    out = []
    i = 0
    while i < len(argv):
        if argv[i] == "--sandbox" and i + 1 < len(argv):
            i += 2
            continue
        if argv[i] == "-c" and i + 1 < len(argv) and str(argv[i + 1]).startswith("sandbox_mode="):
            i += 2
            continue
        out.append(argv[i])
        i += 1
    at = len(out) - 1 if out and out[-1] == "-" else len(out)
    out[at:at] = ["-c", f'default_permissions="{PROFILE}"']
    return out
