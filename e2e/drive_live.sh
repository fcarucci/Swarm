#!/bin/sh
# Drive-mode live acceptance and chaos (plan Tasks 15-19, spec 6.2/6.3), on an isolated board.
# Runbook: ~/work/swarm-autonomy/qa/RUNBOOK.md  (a copy: e2e/drive_live/RUNBOOK.md)
#
#   e2e/drive_live.sh preflight                 what can run today: throwaway board, hub, harness (no driver needed)
#   e2e/drive_live.sh met [--harness real]      MET run: zero coordinator turns, supervisor launches every launch
#   e2e/drive_live.sh unreachable               impossible goal -> UNREACHABLE
#   e2e/drive_live.sh chaos [NAME ...]          the chaos suite (or the named scenarios)
#   e2e/drive_live.sh all                       preflight, met, unreachable, chaos
#   e2e/drive_live.sh pending                   which pieces (6a/6b/8a/8b/8c/11b) the checkout under test has
# Options (after the command): --backend postgres|sqlite|file  --harness fake|real  --budget-usd N
#   --timeout-min N  --interval-s N  --kill-supervisor-once  --keep-pg-logs
# Exit: 0 all pass, 1 any failure, 3 only skips (pending pieces). Never touches the shared board or /tmp.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
QA_ROOT="${SWARM_QA_ROOT:-$HOME/work/swarm-autonomy/qa}"
PY="${SWARM_QA_VENV:-$QA_ROOT/venv}/bin/python"
[ -x "$PY" ] || { echo "drive_live: no venv at $PY: run $HERE/drive_live/setup_qa.sh first" >&2; exit 2; }
cd "$HERE/drive_live" || exit 2
exec "$PY" -B live.py "$@"
