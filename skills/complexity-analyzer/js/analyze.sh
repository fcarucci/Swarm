#!/bin/bash
# JavaScript/TypeScript cognitive complexity analysis.
#
#   analyze.sh <path> [<path> ...]
#
# Prints a TSV ranking:  cognitive<TAB>cyclomatic<TAB>file<TAB>line<TAB>function
# Highest cognitive complexity first. Cyclomatic is "-" when that function did
# not also trip the cyclomatic rule.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOLS="$(PYTHONPATH="$HERE/../lib" python3 -c 'from cxmetrics.core import tools_dir; print(tools_dir() / "node")')"
SCRATCH="$(PYTHONPATH="$HERE/../lib" python3 -c 'from cxmetrics.core import tmp_base; print(tmp_base())')"
WORK="$(mktemp -d "$SCRATCH/jscx-run.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

# Install the manifest's pinned packages with npm ci, into the persistent cache.
if [ ! -x "$TOOLS/node_modules/.bin/eslint" ]; then
  python3 "$HERE/../bin/install_tools.py"
fi

# Mirror the sources into a scratch tree, stripping preprocessor directives.
# Line numbers are preserved, so the report maps back to the originals.
: > "$WORK/manifest"
for target in "$@"; do
  if [ -d "$target" ]; then
    find "$target" -name '*.js' -not -path '*/node_modules/*' >> "$WORK/manifest"
  else
    echo "$target" >> "$WORK/manifest"
  fi
done

mkdir -p "$WORK/src"
n=0
: > "$WORK/map"
while IFS= read -r f; do
  [ -n "$f" ] || continue
  n=$((n + 1))
  python3 "$HERE/strip-directives.py" "$f" "$WORK/src/f${n}.js"
  printf 'f%s.js\t%s\n' "$n" "$f" >> "$WORK/map"
done < "$WORK/manifest"

cp "$HERE/eslint.config.mjs" "$WORK/eslint.config.mjs"
ln -s "$TOOLS/node_modules" "$WORK/node_modules"

# ESLint resolves eslint.config.mjs relative to the CURRENT DIRECTORY, not
# the lint target, so the run must happen from inside $WORK.
( cd "$WORK" && ./node_modules/.bin/eslint src --no-ignore -f json ) \
  > "$WORK/report.json" 2> "$WORK/eslint.err" || true

if [ ! -s "$WORK/report.json" ]; then
  echo "complexity-analyzer: eslint produced no report." >&2
  cat "$WORK/eslint.err" >&2
  exit 1
fi

MAP="$WORK/map" python3 - "$WORK/report.json" <<'PY'
import json, os, re, sys

names = dict(
    line.split("\t")
    for line in open(os.environ["MAP"]).read().splitlines()
    if line
)

NAME_PATTERNS = [
    r"(?:^|\s)function\s+([A-Za-z_$][\w$]*)",        # function foo(
    r"([A-Za-z_$][\w$.]*)\s*[=:]\s*function\b",     # Foo.bar = function(
    r"([A-Za-z_$][\w$.]*)\s*[=:]\s*(?:async\s*)?\(",  # foo = (a) =>
    r"^\s*(?:static\s+|async\s+|get\s+|set\s+)*([A-Za-z_$][\w$]*)\s*\(",  # class method
]

_source_cache = {}


def name_at(path, line):
    """sonarjs's message carries no function name, so recover it from the
    source. Reported line is the function's own line, hence an exact lookup."""
    if path not in _source_cache:
        try:
            _source_cache[path] = open(path, encoding="utf8",
                                       errors="replace").read().split("\n")
        except OSError:
            _source_cache[path] = []
    lines = _source_cache[path]
    if not (0 < line <= len(lines)):
        return "<anonymous>"
    text = lines[line - 1]
    for pattern in NAME_PATTERNS:
        m = re.search(pattern, text)
        if m and m.group(1) not in ("if", "for", "while", "switch", "catch",
                                    "return", "function"):
            return m.group(1)
    return "<anonymous>"


cog, cyc = {}, {}
for entry in json.load(open(sys.argv[1])):
    src = names.get(os.path.basename(entry["filePath"]), entry["filePath"])
    for m in entry["messages"]:
        rule, msg, line = m.get("ruleId"), m["message"], m["line"]
        if rule == "sonarjs/cognitive-complexity":
            score = int(re.search(r"from (\d+)", msg).group(1))
            cog[(src, line)] = (score, name_at(src, line))
        elif rule == "complexity":
            score = int(re.search(r"complexity of (\d+)", msg).group(1))
            cyc[(src, line)] = (score, name_at(src, line))

rows = []
for key, (score, label) in cog.items():
    rows.append((score, cyc.get(key, ("-", None))[0], key[0], key[1], label))
# Functions that trip only the cyclomatic rule still matter; list them with
# a cognitive score of 0 so they rank below any genuine cognitive finding.
for key, (score, label) in cyc.items():
    if key not in cog:
        rows.append((0, score, key[0], key[1], label))

rows.sort(key=lambda r: (-r[0], -(r[1] if r[1] != "-" else 0)))
for r in rows:
    print("%s\t%s\t%s\t%s\t%s" % r)
PY
