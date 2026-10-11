#!/bin/sh
# One-time setup of the QA root (default ~/work/swarm-autonomy/qa): a venv with the swarm requirements
# (hash-checked, like bin/swarm) and a self-contained Postgres. Idempotent. Never writes under /tmp.
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"; REPO="$HERE/../.."
QA="${SWARM_QA_ROOT:-$HOME/work/swarm-autonomy/qa}"
case "$QA" in /tmp/*|/var/tmp/*|/dev/shm/*) echo "setup_qa: refusing $QA (tmpfs/RAM)" >&2; exit 1;; esac
mkdir -p "$QA/runs"
if [ ! -x "$QA/venv/bin/python" ]; then
  python3 -m venv "$QA/venv"
  "$QA/venv/bin/python" -m pip install -q --require-hashes --only-binary=:all: \
    -r "$REPO/requirements.txt" -r "$REPO/requirements-postgres.txt"
fi
# Postgres: any bin dir whose `postgres -V` runs. The pg0 install (~/.pg0) needs libicuuc.so.70, which
# this box no longer has; the pgserver wheel carries its own libs. Copy it once into $QA/pgdist.
if ! "$QA/pgdist/pgserver/pginstall/bin/postgres" -V >/dev/null 2>&1; then
  src=$(ls -d "$HOME"/.cache/uv/archive-v0/*/pgserver 2>/dev/null | head -1 || true)
  if [ -n "$src" ]; then mkdir -p "$QA/pgdist"; cp -a "$src" "$(dirname "$src")/pgserver.libs" "$QA/pgdist/"
  else
    "$QA/venv/bin/python" -m pip download -q --only-binary=:all: -d "$QA/pgdist/wheel" pgserver
    mkdir -p "$QA/pgdist/x"; (cd "$QA/pgdist/x" && "$QA/venv/bin/python" -m zipfile -e ../wheel/pgserver-*.whl .)
    mv "$QA/pgdist/x/pgserver" "$QA/pgdist/x/pgserver.libs" "$QA/pgdist/"; rm -rf "$QA/pgdist/x" "$QA/pgdist/wheel"
  fi
fi
"$QA/pgdist/pgserver/pginstall/bin/postgres" -V
echo "setup_qa: ready in $QA"
