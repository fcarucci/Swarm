#!/usr/bin/env bash
# Three independent offline suites; PostgreSQL follows them. Shared CPU load lasts throughout.
set -u
if [[ "${1:-}" == "--help" || $# -lt 1 || $# -gt 3 ]]; then
    echo 'Usage: PYTHON=/path/to/python PG_BIN=/path/to/pg/bin scripts/stress-tests.sh OUTPUT [OFFLINE_RUNS=15] [POSTGRES_RUNS=8]'
    echo 'Optional: STRESS_LOAD_WORKERS=4 STRESS_TIMEOUT=7200; OUTPUT must be new and outside /tmp.'
    [[ "${1:-}" == "--help" ]] && exit 0
    exit 2
fi
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python="${PYTHON:-$root/.venv/bin/python}"
if [[ ! -x "$python" ]]; then python="${PYTHON:-python3}"; fi
runs="${2:-15}"
pg_runs="${3:-8}"
workers="${STRESS_LOAD_WORKERS:-4}"
timeout="${STRESS_TIMEOUT:-7200}"
for number in "$runs" "$pg_runs" "$workers" "$timeout"; do
    [[ "$number" =~ ^[0-9]+$ ]] || { echo 'Run/load/timeout values must be integers.' >&2; exit 2; }
done
(( runs > 0 && pg_runs > 0 && timeout > 0 && workers <= 4 )) || exit 2
output="$("$python" -c 'import sys; from pathlib import Path; p=Path(sys.argv[1]).expanduser().resolve(); assert p!=Path("/tmp") and Path("/tmp") not in p.parents, "output must be outside /tmp"; print(p)' "$1")" || exit 2
mkdir -m 700 "$output" || exit 2
load_pids=()
test_pids=()
cleanup() {
    for pid in "${test_pids[@]}"; do kill -TERM "$pid" 2>/dev/null || true; done
    for pid in "${test_pids[@]}"; do wait "$pid" 2>/dev/null || true; done
    for pid in "${load_pids[@]}"; do kill -TERM "$pid" 2>/dev/null || true; done
    for pid in "${load_pids[@]}"; do wait "$pid" 2>/dev/null || true; done
}
trap cleanup EXIT
trap 'exit 130' INT TERM
for ((i=0; i<workers; i++)); do
    "$python" -c 'while True: pass' &
    load_pids+=("$!")
done
"$python" -c 'import json,sys; from pathlib import Path; Path(sys.argv[1]).write_text(json.dumps(dict(load_workers=len(sys.argv[2:]),load_pids=[int(p) for p in sys.argv[2:]],offline_parallelism=3),indent=2))' "$output/matrix.json" "${load_pids[@]}" || exit 2
failed=0
for backend in memory file sqlite; do
    "$python" "$root/scripts/stress-tests.py" --backends "$backend" --runs "$runs" \
        --load-workers 0 --timeout "$timeout" --output "$output/$backend" \
        > "$output/$backend-console.log" 2>&1 &
    test_pids+=("$!")
done
for pid in "${test_pids[@]}"; do wait "$pid" || failed=1; done
test_pids=()
pg_args=()
if [[ -n "${PG_BIN:-}" ]]; then pg_args=(--pg-bin "$PG_BIN"); fi
"$python" "$root/scripts/stress-tests.py" --backends postgres --postgres-runs "$pg_runs" \
    --load-workers 0 --timeout "$timeout" --output "$output/postgres" "${pg_args[@]}" \
    > "$output/postgres-console.log" 2>&1 &
test_pids=("$!")
wait "${test_pids[0]}" || failed=1
test_pids=()
"$python" - "$output" <<'PY' || failed=1
from collections import Counter
import json
from pathlib import Path
import sys
root=Path(sys.argv[1])
failures=[]
outcomes=[]
for backend in ('memory','file','sqlite','postgres'):
    directory=root/backend
    if (directory/'failures.jsonl').exists():
        failures.extend(json.loads(row) for row in (directory/'failures.jsonl').read_text().splitlines())
    if (directory/'outcomes.json').exists():
        outcomes.extend(json.loads((directory/'outcomes.json').read_text()))
(root/'failures.jsonl').write_text(''.join(json.dumps(row)+'\n' for row in failures))
(root/'outcomes.json').write_text(json.dumps(outcomes,indent=2))
counts=Counter((r['backend'],r['test_id']) for r in
               {(r['backend'],r['run'],r['test_id']):r for r in failures}.values())
runs=Counter(r['backend'] for r in outcomes)
lines=['| Backend | Test id | Failed runs / completed runs |','| --- | --- | --- |']
lines.extend(f'| {backend} | {test} | {count}/{runs[backend]} |'
             for (backend,test),count in sorted(counts.items()))
for backend,count in sorted(runs.items()):
    passing=sum(r['returncode']==0 for r in outcomes if r['backend']==backend)
    lines.append(f'\n{backend}: {passing}/{count} full runs passed.')
text='\n'.join(lines)+'\n'
(root/'flakes.md').write_text(text)
print(text)
PY
exit "$failed"
