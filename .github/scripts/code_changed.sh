#!/usr/bin/env bash
# Print "true" when anything other than documentation changed between two commits, else "false".
# Usage: code_changed.sh BASE HEAD   (run inside a checkout that has both commits)
#
# Documentation: any *.md file, everything under docs/, and LICENSE. Exceptions that count as
# code: *.md under tests/ and e2e/ (fixtures), and a change to the YAML frontmatter of an *.md
# file under skills/ (SKILL.md name/description are what the plugin loads). Plugin manifests,
# *.toml examples and everything else count as code.
set -euo pipefail
base=$1 head=$2

# The frontmatter block (the leading '---' line through the closing '---'), or nothing.
frontmatter() {
  git show "$1:$2" 2>/dev/null | awk 'NR == 1 && $0 != "---" { exit } { print } NR > 1 && $0 == "---" { exit }' || true
}

is_doc() {
  case "$1" in
    tests/* | e2e/*) return 1 ;;
    docs/* | LICENSE) return 0 ;;
    skills/*.md) [ "$(frontmatter "$base" "$1")" = "$(frontmatter "$head" "$1")" ] ;;
    *.md) return 0 ;;
    *) return 1 ;;
  esac
}

files=$(git diff --name-only --no-renames "$base" "$head")
while IFS= read -r f; do
  [ -z "$f" ] && continue
  if ! is_doc "$f"; then
    echo "code change: $f" >&2
    echo true
    exit 0
  fi
done <<< "$files"
echo "docs-only change (or no change)" >&2
echo false
