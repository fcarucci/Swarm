#!/bin/bash
# Example trusted wrapper: command = ["/absolute/path/codex-via-pct.sh", "pve-seed"]
# Requires key-based SSH to a Proxmox cluster node and a shared workdir at the same path in CT 118.
# The codex user's Swarm config/hooks must already reach the same board. No credentials live here.
set -euo pipefail
seed=${1:?usage: codex-via-pct.sh PVE-SEED exec [codex arguments...]}
shift

# Query any cluster member for CT 118's current node; -n leaves the launch brief on stdin untouched.
node=$(ssh -n -o BatchMode=yes "root@$seed" pvesh get /cluster/resources --type vm --output-format json |
    python3 -c 'import json, sys
rows = [r for r in json.load(sys.stdin) if r.get("type") == "lxc" and r.get("vmid") == 118 and r.get("status") == "running"]
if len(rows) != 1:
    sys.exit("CT 118 is not running on exactly one node")
print(rows[0]["node"])')
[[ $node =~ ^[a-zA-Z0-9][a-zA-Z0-9.-]*$ ]] || { echo "Invalid PVE node" >&2; exit 1; }

# SSH takes a shell command: quote every argv element, including the workdir and model.
remote=$(python3 -c 'import shlex, sys
print(shlex.join(["pct", "exec", "118", "--", "runuser", "-u", "codex", "--", "bash", "-lc",
    "cd -- \"$1\" || exit; shift; exec codex -c mcp_servers.playwright.enabled=false \"$@\"",
    "codex-via-pct", *sys.argv[1:]]))' "$PWD" "$@")
exec ssh -T -o BatchMode=yes "root@$node" "$remote"
