#!/usr/bin/env bash
# Builds release notes (markdown) for a swarm plugin release: the install one-liner (GitHub +
# GitHub raw URL), the "upgrade every host at once" note, then a changelog built from commits
# since the previous vX.Y.Z tag, grouped by type where the messages allow it.
#
# Usage:
#   scripts/release-notes.sh [tag]
#
# `tag` defaults to the most recent vX.Y.Z tag reachable from HEAD; if there is no such tag yet,
# HEAD is used as the release point and every commit reachable from it is listed (first release).
# Run from anywhere; paths are resolved relative to this script's repo.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

TAG="${1:-}"
if [ -z "$TAG" ]; then
  TAG="$(git describe --tags --match 'v*' --abbrev=0 2>/dev/null || true)"
fi

if [ -n "$TAG" ] && git rev-parse -q --verify "refs/tags/$TAG" >/dev/null; then
  END="refs/tags/$TAG"
  DISPLAY_TAG="$TAG"
else
  END="HEAD"
  DISPLAY_TAG="${TAG:-$(git rev-parse --short HEAD)}"
fi

PREV=""
if git rev-parse -q --verify "${END}^" >/dev/null 2>&1; then
  PREV="$(git describe --tags --match 'v*' --abbrev=0 "${END}^" 2>/dev/null || true)"
fi

if [ -n "$PREV" ]; then
  RANGE="${PREV}..${END}"
else
  RANGE="${END}"
fi

# ---------------------------------------------------------------------- install / upgrade notes
#
# Hardcoded rather than scraped from README.md: the README's structure/wording is owned by
# another agent/PR and is free to change shape (e.g. move to docs/REFERENCE.md) without breaking
# release notes. Releases and the installer live on GitHub.

INSTALL_LINE="curl -fsSL https://raw.githubusercontent.com/fcarucci/Swarm/main/install.sh | bash"

UPGRADE_NOTE="Upgrade every host that shares a board together (\`swarm upgrade\`); see README.md, Upgrading."

# ---------------------------------------------------------------------------- classify commits

categorize() {
  # $1 = lowercased subject; prints one of Added/Fixed/Removed/Documentation/Changed/Other
  local s="$1"
  if printf '%s' "$s" | grep -qiE '(^|[^a-z])(fix|fixes|fixed|bugfix|bug)([^a-z]|$)'; then
    echo "Fixed"
  elif printf '%s' "$s" | grep -qiE '(^|[^a-z])(add|adds|added|new|feat|feature|introduce|introduces)([^a-z]|$)'; then
    echo "Added"
  elif printf '%s' "$s" | grep -qiE '(^|[^a-z])(remove|removes|removed|drop|drops|dropped|deprecate|deprecated)([^a-z]|$)'; then
    echo "Removed"
  elif printf '%s' "$s" | grep -qiE '(^|[^a-z])(doc|docs|readme|changelog)([^a-z]|$)'; then
    echo "Documentation"
  elif printf '%s' "$s" | grep -qiE '(^|[^a-z])(refactor|refactors|refactored|clean|cleanup|simplify|simplifies|harden|hardening|rename|renames|renamed)([^a-z]|$)'; then
    echo "Changed"
  else
    echo "Other"
  fi
}

LOG_FILE="$(mktemp)"
trap 'rm -f "$LOG_FILE"' EXIT
git log --no-merges --pretty=tformat:'%h%x09%s' "$RANGE" -- > "$LOG_FILE"

TOTAL=$(wc -l < "$LOG_FILE" | tr -d ' ')

order="Added Fixed Changed Removed Documentation Other"
matched_any=0
while IFS=$'\t' read -r sha subject; do
  [ -n "$sha" ] || continue
  cat="$(categorize "$(printf '%s' "$subject" | tr '[:upper:]' '[:lower:]')")"
  if [ "$cat" != "Other" ]; then matched_any=1; fi
done < "$LOG_FILE"

# ------------------------------------------------------------------------------------ changelog
#
# The release notes' "What's changed" comes from CHANGELOG.md's `## [x.y.z]` section for this
# tag, read as it was at the tag (falling back to the working tree for tags that predate the
# file). A tag with no section fails: write the entry before tagging. Set
# RELEASE_NOTES_COMMITS=1 to build the section from the commit list instead.

changelog_section() {
  local ver="${DISPLAY_TAG#v}" text=""
  if [ "$END" != "HEAD" ] && git cat-file -e "$END:CHANGELOG.md" 2>/dev/null; then
    text="$(git show "$END:CHANGELOG.md")"
  elif [ -f CHANGELOG.md ]; then
    text="$(cat CHANGELOG.md)"
  fi
  # Plain awk only (no GNU sed tricks: this also runs on macOS/BSD). Strips CRs, matches the
  # heading literally including the closing bracket (so 0.1.1 never matches 0.1.10), and trims
  # leading and trailing blank lines.
  printf '%s\n' "$text" | awk -v v="$ver" '
    { sub(/\r$/, "") }
    /^## \[/ { if (on) exit; on = (index($0, "## [" v "]") == 1); next }
    !on { next }
    $0 ~ /^[ \t]*$/ { if (started) blanks++; next }
    { for (; blanks > 0; blanks--) print ""; started = 1; print }
  '
}

# ------------------------------------------------------------------------------------ render

footer() {
  echo
  echo "## Install"
  echo
  echo "\`$INSTALL_LINE\`"
  echo
  echo "$UPGRADE_NOTE"
}

echo "## What's changed"
echo

CL="$(changelog_section)"
if [ -z "${RELEASE_NOTES_COMMITS:-}" ] && [ -n "$CL" ]; then
  printf '%s\n' "$CL"
  echo
  echo "Full changelog: https://github.com/fcarucci/Swarm/blob/main/CHANGELOG.md"
  footer
  exit 0
elif [ -z "${RELEASE_NOTES_COMMITS:-}" ] && [ -n "$TAG" ]; then
  echo "error: CHANGELOG.md has no '## [${DISPLAY_TAG#v}]' section; add one before tagging" >&2
  exit 1
fi

if [ "$TOTAL" -eq 0 ]; then
  echo "No commits."
elif [ "$matched_any" -eq 1 ]; then
  for cat in $order; do
    section=""
    while IFS=$'\t' read -r sha subject; do
      [ -n "$sha" ] || continue
      c="$(categorize "$(printf '%s' "$subject" | tr '[:upper:]' '[:lower:]')")"
      [ "$c" = "$cat" ] || continue
      section="${section}- ${subject} (${sha})
"
    done < "$LOG_FILE"
    if [ -n "$section" ]; then
      echo "### $cat"
      echo
      printf '%s' "$section"
      echo
    fi
  done
else
  # No commit message matched a recognizable keyword: a plain list beats a wall of "Other".
  while IFS=$'\t' read -r sha subject; do
    [ -n "$sha" ] || continue
    echo "- ${subject} (${sha})"
  done < "$LOG_FILE"
fi

if [ -z "$PREV" ]; then
  echo
  echo "_First release: every commit up to ${DISPLAY_TAG} is listed above._"
fi

footer
