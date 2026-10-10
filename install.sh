#!/usr/bin/env bash
# swarm one-shot installer.
#
#   curl -fsSL https://raw.githubusercontent.com/fcarucci/Swarm/main/install.sh | bash
#   curl -fsSL https://raw.githubusercontent.com/fcarucci/Swarm/main/install.sh | sudo bash   # every user on this machine
#   curl -fsSL https://raw.githubusercontent.com/fcarucci/Swarm/main/install.sh | bash -s -- --host codex
#   bash install.sh [flags]
#
# What it does, per detected host (claude and/or codex):
#   1. add or update the swarm marketplace
#   2. install (or update) the swarm plugin
#   3. activate it (Claude: enable; Codex: print the manual /hooks trust step)
#   4. run `swarm bootstrap --host <h>` with the freshly installed plugin's own bin/swarm (found
#      in the host's plugin cache, never through ~/.local/bin/swarm, which may point at another
#      tree); bootstrap repoints that launcher at the new plugin
#   5. run `swarm migrate` (retires an old ~/.claude/skills/swarm install; refuses while a swarm
#      job is active)
#   6. run `swarm doctor`
#
# Every step is idempotent: re-running this script is safe.
#
# Two modes:
#
#   * Normal (not root): installs for the invoking user only. It never calls sudo or su. If it
#     sees other OS users with a claude/codex install, it lists them and prints the exact sudo
#     command that covers them.
#
#   * All users (run as root: `curl ... | sudo bash`, `sudo bash install.sh`; --all-users says
#     the same thing explicitly and refuses without root): finds every human user (Linux: uid >=
#     1000 with a real login shell and home; macOS: /Users/* except Shared) who has a ~/.claude or
#     ~/.codex config home, or a claude/codex binary in a per-user bin dir, and re-runs this same
#     installer AS that user (`sudo -u <user> -H env HOME=<home> bash <copy> --current-user --yes
#     <host flags>`, plus XDG_RUNTIME_DIR=/run/user/<uid> when that exists, or `su - <user> -c
#     ...` where there's no sudo). Root itself never writes into a user's home: the copy, and
#     root's own scratch dir, live in root-owned mktemp dirs under /tmp (copy: dir 0755, file
#     0644), removed on exit. Each user's run does its own detection, marketplace, install, enable,
#     bootstrap, migrate and doctor; one user refusing (active swarm job, board config to fill
#     in) or failing doesn't stop the others. A USER x HOST summary table closes the run.
#
# Codex's hook trust (/hooks) stays manual in both modes: Codex has no supported non-interactive
# way to make a persistent trust decision (--dangerously-bypass-hook-trust only covers one
# `codex exec`), so the exact step is printed for each user instead.
#
# The board defaults to plain files (no server, no credentials): with no config yet, bootstrap
# writes one with `backend = "file"` and the run carries on through migrate and doctor. Board
# credentials are still never invented: a shared Postgres board's [database] settings are the
# user's step. Should a config step still need a human (bootstrap reporting "manual"), the run
# prints what to fix and stops before migrate/doctor.
#
# IMPORTANT -- schema v9 upgrade: this release moves the board's schema to v9. If the board this
# host uses is shared with other hosts (another machine, or the other OS user on this one, e.g.
# claude and codex on the same shared host), upgrade every one of them together. An older client
# left behind on the old schema does not just miss features: see docs/REFERENCE.md "Upgrading:
# this version needs schema v9, on every host at once".
#
# Everything lives in functions and the last line calls main: when piped into bash, nothing runs
# until the whole script has arrived (a truncated download does nothing), and a root run can
# rebuild an exact copy of itself for the per-user runs with `declare -f`.
set -euo pipefail

# --------------------------------------------------------------------------- globals

init_globals() {
  INSTALL_URL="https://raw.githubusercontent.com/fcarucci/Swarm/main/install.sh"
  DEFAULT_MARKETPLACE="https://github.com/fcarucci/Swarm.git"
  MARKETPLACE_NAME="swarm"
  PLUGIN_SPEC="swarm@swarm"
  MIN_PY_MAJOR=3
  MIN_PY_MINOR=11
  RESULT_PREFIX="swarm-install-result:"

  UPGRADE_NOTE="swarm 0.1.0 upgrades the board schema to v9. If the board is shared by other hosts \
(another machine, or the claude/codex OS users on this one), upgrade ALL of them together, at \
around the same time: an old client left behind fails in specific ways (garbage collection FK \
violations, a stuck supervisor lock, transcripts stuck showing 'capture failed') -- see \
docs/REFERENCE.md \"Upgrading: this version needs schema v9, on every host at once\"."

  TMP_DIR=""
  COPY_DIR=""
  HOST_ARG=""
  MARKETPLACE="$DEFAULT_MARKETPLACE"
  MARKETPLACE_GIVEN=0
  CHANNEL="release"      # release: the newest vX.Y.Z tag; main: the tip of main
  CHANNEL_GIVEN=0
  REF_GIVEN=""           # --ref <tag|branch>: an explicit ref, overrides the channel
  PIN_REF=""             # what resolve_channel settled on ("" = the default branch, unpinned)
  PIN_ACTIVE=0           # 1 when the marketplace is added pinned (not for a local path)
  ASSUME_YES=0
  ALL_USERS=0
  CURRENT_USER_ONLY=0
  FORCE=0
  NO_COLOR_FLAG=0
  COLOR=0
  C_RESET=""; C_BOLD=""; C_GREEN=""; C_CYAN=""; C_YELLOW=""; C_RED=""; C_BLUE=""
  CLAUDE_BIN=""
  CODEX_BIN=""
  HOSTS=""
  CONFIG_NEEDS_ATTENTION=0
  CONFIG_DETAIL=""
  DOCTOR_FAILED=0
  STATUS_CLAUDE=""
  STATUS_CODEX=""
  RUN_RESULT=""
}

# --------------------------------------------------------------------------- colour
#
# Colour only when our own stdout is a real terminal (never when piped or captured, which is
# what `$(...)` in the tests does too), and never when NO_COLOR is set (https://no-color.org) or
# --no-color was given. tput is tried first (works with whatever the terminal actually supports);
# a fixed ANSI escape fallback covers a tty with no tput (bash 3.2 has no other requirement here).

setup_color() {
  COLOR=0
  if [ "$NO_COLOR_FLAG" != "1" ] && [ -z "${NO_COLOR:-}" ] && [ -t 1 ]; then
    COLOR=1
  fi
  if [ "$COLOR" = "1" ] && command -v tput >/dev/null 2>&1 && tput colors >/dev/null 2>&1; then
    C_RESET="$(tput sgr0 2>/dev/null)"; C_BOLD="$(tput bold 2>/dev/null)"
    C_GREEN="$(tput setaf 2 2>/dev/null)"; C_CYAN="$(tput setaf 6 2>/dev/null)"
    C_YELLOW="$(tput setaf 3 2>/dev/null)"; C_RED="$(tput setaf 1 2>/dev/null)"
    C_BLUE="$(tput setaf 4 2>/dev/null)"
  elif [ "$COLOR" = "1" ]; then
    C_RESET=$'\033[0m'; C_BOLD=$'\033[1m'
    C_GREEN=$'\033[32m'; C_CYAN=$'\033[36m'; C_YELLOW=$'\033[33m'; C_RED=$'\033[31m'; C_BLUE=$'\033[34m'
  else
    C_RESET=""; C_BOLD=""; C_GREEN=""; C_CYAN=""; C_YELLOW=""; C_RED=""; C_BLUE=""
  fi
}

# Recolours the known status words (ok/changed/manual/skipped/refused/failed, OK/FAIL/WARN) in
# text piped through it -- `swarm bootstrap`/`migrate`/`doctor` output, captured (so it is never
# a tty to them) and re-printed by this script. A no-op pass-through when COLOR=0.
paint_lines() {
  # Colours only the status word itself (OK/FAIL/WARN, or ok/changed/manual/skipped/refused/
  # failed), found as a whole word anywhere on the line, by splicing the SGR codes directly into
  # the original string at that exact position -- never through awk's $1/$2 field reassignment,
  # which rebuilds the whole line from OFS and collapses the fixed-width column padding
  # format_steps()/format_checks() use for alignment.
  if [ "$COLOR" != "1" ]; then cat; return 0; fi
  awk -v ok="$C_GREEN" -v ch="$C_CYAN" -v wn="$C_YELLOW" -v fl="$C_RED" -v rs="$C_RESET" '
    {
      line = $0
      if (match(line, /(^|[ \t])(OK|FAIL|WARN|ok|changed|manual|skipped|refused|failed)([ \t]|$)/)) {
        full = substr(line, RSTART, RLENGTH)
        w = full
        gsub(/^[ \t]+|[ \t]+$/, "", w)
        if (w == "OK" || w == "ok") code = ok
        else if (w == "FAIL" || w == "failed") code = fl
        else if (w == "changed") code = ch
        else code = wn
        pos = index(full, w)
        pre = substr(full, 1, pos - 1)
        post = substr(full, pos + length(w))
        newfull = pre code w rs post
        line = substr(line, 1, RSTART - 1) newfull substr(line, RSTART + RLENGTH)
      }
      print line
    }'
}

# A single status-ish word ($1), coloured (ok/yes green, changed cyan, manual/refused/skipped/
# no/WARN/needs-board-config yellow, FAIL/failed/no-cli/doctor-FAIL red), padded to $2 columns
# first (so ANSI codes never throw off table alignment) when $2 is given.
paint_word() {
  w="$1"; width="${2:-0}"
  padded="$w"
  [ "$width" -gt 0 ] 2>/dev/null && padded="$(printf "%-${width}s" "$w")"
  if [ "$COLOR" != "1" ]; then printf '%s' "$padded"; return 0; fi
  code=""
  case "$w" in
    ok|ok\(*|yes|OK) code="$C_GREEN" ;;
    changed) code="$C_CYAN" ;;
    manual|manual\(*|refused|refused\(*|skipped|skipped\(*|no|needs-board-config|WARN) code="$C_YELLOW" ;;
    FAIL|doctor-FAIL|failed|failed\(*|no-cli) code="$C_RED" ;;
  esac
  [ -n "$code" ] && printf '%s%s%s' "$code" "$padded" "$C_RESET" || printf '%s' "$padded"
}

# --------------------------------------------------------------------------- output helpers

log()  { printf '%s==>%s %s\n' "$C_BOLD$C_BLUE" "$C_RESET" "$*"; }
warn() { printf '%sswarm-install: warning:%s %s\n' "$C_YELLOW" "$C_RESET" "$*" >&2; }
die()  { printf '%sswarm-install:%s %s\n' "$C_RED" "$C_RESET" "$*" >&2; exit 1; }

usage() {
  cat <<EOF
swarm installer

$UPGRADE_NOTE

Usage:
  curl -fsSL $INSTALL_URL | bash
  curl -fsSL $INSTALL_URL | sudo bash   # every user on this machine
  curl -fsSL $INSTALL_URL | bash -s -- --host codex
  bash install.sh [flags]

Flags:
  --host claude|codex|both   install only for this host (default: every claude/codex CLI found,
                              scanning PATH plus common install locations)
  --marketplace <url|path>   marketplace to add (default: $DEFAULT_MARKETPLACE).
                              A local path is a frozen tree, for testing before a push.
  --channel release|main     which revision to install: release = the newest vX.Y.Z tag
                              (default), main = the tip of the main branch. If no tag can be
                              found (or git ls-remote fails) it falls back to main with a warning.
  --main                     shorthand for --channel main
  --ref <tag|branch>         install exactly this tag or branch (overrides --channel)
  --yes                      never prompt; assume yes (required when there is no TTY)
  --all-users                install for every human user on this machine who has claude or
                              codex; requires root (this is also what running as root does)
  --current-user             install only for the invoking user, even as root (each per-user
                              run of an all-users install gets this)
  --force                    pass --force to 'swarm migrate', so a stale local swarm-job marker
                              left on this machine (the board already closed the job) doesn't
                              block it; before forcing, prints each overridden job and whether
                              it is still open on the board (swarm status --all), with a warning
                              if it is. In --all-users mode, passed through to every user's run.
  --no-color                 never colour the output (also off automatically when not a
                              terminal, e.g. piped or captured; NO_COLOR has the same effect)
  -h, --help                 print this help and exit

Run as a normal user, it installs for you only and never calls sudo or su; it lists any other
users it sees with claude/codex and the sudo command that covers them. Run as root, it re-runs
itself AS each of those users (sudo -u, or su where there's no sudo) -- root never writes into
a user's home -- and ends with a USER x HOST summary. Codex's /hooks trust step stays manual;
the exact step is printed for each user.

Every step (marketplace add/update, plugin install, activate, bootstrap, migrate, doctor) is
idempotent: re-running this script is safe.
EOF
}

# --------------------------------------------------------------------------- scratch dir / exit

cleanup() {
  [ -n "${TMP_DIR:-}" ] && rm -rf "$TMP_DIR" 2>/dev/null
  [ -n "${COPY_DIR:-}" ] && rm -rf "$COPY_DIR" 2>/dev/null
  return 0
}

on_exit() {
  rc=$?
  # A per-user run (--current-user) always ends with its result lines, on success, refusal or
  # failure alike, so the root run that launched it can build its summary table.
  if [ "${CURRENT_USER_ONLY:-0}" = "1" ]; then emit_results "$rc"; fi
  cleanup
}

make_tmp_dir() {   # $1 = 1 when running as root
  # As root, always a fresh dir under /tmp: sudo may keep the invoking user's HOME (macOS does
  # by default), and root must not create or write ~/.cache in someone else's home.
  if [ "${1:-0}" = "1" ]; then
    TMP_DIR="$(mktemp -d /tmp/swarm-install.XXXXXX)"
    return 0
  fi
  TMP_BASE="${TMPDIR:-$HOME/.cache}"
  mkdir -p "$TMP_BASE"
  TMP_DIR="$(mktemp -d "$TMP_BASE/swarm-install.XXXXXX")"
}

# --------------------------------------------------------------------------- args

parse_args() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --host)
        [ $# -ge 2 ] || die "--host needs a value (claude, codex or both)"
        HOST_ARG="$2"; shift 2 ;;
      --host=*)
        HOST_ARG="${1#--host=}"; shift ;;
      --marketplace)
        [ $# -ge 2 ] || die "--marketplace needs a value (a URL or a local path)"
        MARKETPLACE="$2"; MARKETPLACE_GIVEN=1; shift 2 ;;
      --marketplace=*)
        MARKETPLACE="${1#--marketplace=}"; MARKETPLACE_GIVEN=1; shift ;;
      --channel)
        [ $# -ge 2 ] || die "--channel needs a value (release or main)"
        CHANNEL="$2"; CHANNEL_GIVEN=1; shift 2 ;;
      --channel=*)
        CHANNEL="${1#--channel=}"; CHANNEL_GIVEN=1; shift ;;
      --main)
        CHANNEL="main"; CHANNEL_GIVEN=1; shift ;;
      --ref)
        [ $# -ge 2 ] || die "--ref needs a value (a tag or branch)"
        REF_GIVEN="$2"; shift 2 ;;
      --ref=*)
        REF_GIVEN="${1#--ref=}"; shift ;;
      --yes)
        ASSUME_YES=1; shift ;;
      --all-users)
        ALL_USERS=1; shift ;;
      --current-user)
        CURRENT_USER_ONLY=1; shift ;;
      --force)
        FORCE=1; shift ;;
      --no-color)
        NO_COLOR_FLAG=1; shift ;;
      -h|--help)
        usage; exit 0 ;;
      *)
        die "unknown argument: $1 (see --help)" ;;
    esac
  done

  case "$HOST_ARG" in
    ""|claude|codex|both) : ;;
    *) die "--host must be claude, codex or both (got: $HOST_ARG)" ;;
  esac
  case "$CHANNEL" in
    release|main) : ;;
    *) die "--channel must be release or main (got: $CHANNEL)" ;;
  esac
  case "$REF_GIVEN" in
    ""|*[!A-Za-z0-9._/+-]*|-*) [ -z "$REF_GIVEN" ] || die "--ref must be a tag or branch name (got: $REF_GIVEN)" ;;
  esac
  if [ "$ALL_USERS" = "1" ] && [ "$CURRENT_USER_ONLY" = "1" ]; then
    die "--all-users and --current-user contradict each other; pick one"
  fi
}

# Host flags to hand on (to a per-user run, or into a printed command). One word per line.
host_flags() {
  [ -n "$HOST_ARG" ] && printf '%s\n' --host "$HOST_ARG"
  [ "$MARKETPLACE_GIVEN" = "1" ] && printf '%s\n' --marketplace "$MARKETPLACE"
  [ "$CHANNEL_GIVEN" = "1" ] && printf '%s\n' --channel "$CHANNEL"
  [ -n "$REF_GIVEN" ] && printf '%s\n' --ref "$REF_GIVEN"
  [ "$FORCE" = "1" ] && printf '%s\n' --force
  return 0
}

host_flags_quoted() {   # the same, shell-quoted on one line (leading space), for printed commands
  out=""
  while IFS= read -r w; do
    [ -n "$w" ] && out="$out $(printf '%q' "$w")"
  done <<EOF
$(host_flags)
EOF
  printf '%s' "$out"
}

# --------------------------------------------------------------------------- confirm (TTY only)

confirm() {
  # Reads from /dev/tty, never stdin: stdin is this script itself when piped into bash. Fails
  # clearly, and never guesses, when there is no controlling terminal (--yes is the way around
  # that, e.g. from a script or CI).
  prompt="$1"
  [ "$ASSUME_YES" = "1" ] && return 0
  if ! exec 3<>/dev/tty 2>/dev/null; then
    die "no TTY available to ask \"$prompt\" (stdin is the installer script itself, so prompts read /dev/tty); re-run with --yes for a non-interactive install, or run this from an interactive terminal"
  fi
  printf '%s [y/N] ' "$prompt" >&3
  IFS= read -r reply <&3 || reply=""
  exec 3<&- 3>&- 2>/dev/null || true
  case "$reply" in
    [Yy]|[Yy][Ee][Ss]) return 0 ;;
    *) return 1 ;;
  esac
}

# --------------------------------------------------------------------------- preflight

preflight() {
  command -v git >/dev/null 2>&1 || die "git not found on PATH (needed for a local --marketplace clone/checkout, and by the CLIs)"

  command -v python3 >/dev/null 2>&1 || die "python3 not found on PATH; swarm needs python3 >= ${MIN_PY_MAJOR}.${MIN_PY_MINOR} (its config is read with tomllib)"
  py_ver="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
  py_major="${py_ver%%.*}"; py_minor="${py_ver#*.}"
  if [ "$py_major" -lt "$MIN_PY_MAJOR" ] || { [ "$py_major" -eq "$MIN_PY_MAJOR" ] && [ "$py_minor" -lt "$MIN_PY_MINOR" ]; }; then
    die "python3 $py_ver found; swarm needs >= ${MIN_PY_MAJOR}.${MIN_PY_MINOR} (its config is read with tomllib)"
  fi
  log "preflight: python3 $py_ver, git $(git --version 2>/dev/null | head -n1 | sed 's/^git version //')"

  for h in $HOSTS; do
    b="$(host_bin "$h")"
    v="$("$b" --version 2>/dev/null </dev/null | head -n1 || true)"
    log "preflight: $h -> $b${v:+ ($v)}"
  done

  case "$MARKETPLACE" in
    git@*:*|ssh://*)
      sshtarget="$MARKETPLACE"
      case "$sshtarget" in
        ssh://*) sshtarget="${sshtarget#ssh://}"; sshtarget="${sshtarget%%/*}" ;;
        *) sshtarget="${sshtarget%%:*}" ;;
      esac
      command -v ssh >/dev/null 2>&1 || die "ssh not found on PATH, needed to reach the marketplace $MARKETPLACE"
      log "preflight: checking ssh access to $sshtarget"
      out="$(ssh -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new "$sshtarget" </dev/null 2>&1 || true)"
      case "$out" in
        *"Permission denied"*)
          die "ssh access to $sshtarget failed (Permission denied): add your SSH key to the git host, or pass --marketplace with a local path" ;;
        *"Could not resolve hostname"*|*"Connection refused"*|*"Connection timed out"*|*"No route to host"*)
          die "can't reach $sshtarget over ssh: $out" ;;
      esac
      # A git server that answers (even by refusing a shell, which is normal and not an error
      # here) is reachable; the real access check happens at the actual clone/add below.
      ;;
    *) : ;;
  esac
}

# --------------------------------------------------------------------------- host detection
#
# A binary can be on PATH, or only in a common install location; the plugin lives in a config
# home (CLAUDE_CONFIG_DIR/~/.claude, CODEX_HOME/~/.codex), not per binary, so once a binary is
# found for a host we act on that host's one config home regardless of which of its binaries we
# used to find it (no per-binary duplicate work).

user_bin_dirs() {   # $1 = a home dir -> the per-user install locations under it, ":"-separated
  h="$1"
  d="$h/.local/bin:$h/.claude/local:$h/.npm-global/bin:$h/.bun/bin:$h/.volta/bin:$h/bin"
  for nv in "$h"/.nvm/versions/node/*/bin; do
    [ -d "$nv" ] && d="$d:$nv"
  done
  printf '%s' "$d"
}

extra_bin_dirs() {
  # Common install locations beyond PATH, checked in addition to it (bash 3.2: no arrays).
  d="$(user_bin_dirs "$HOME"):/usr/local/bin:/opt/homebrew/bin"
  if command -v npm >/dev/null 2>&1; then
    npmbin="$(npm bin -g 2>/dev/null </dev/null || true)"
    [ -n "$npmbin" ] && d="$d:$npmbin"
    npmprefix="$(npm config get prefix 2>/dev/null </dev/null || true)"
    [ -n "$npmprefix" ] && [ "$npmprefix" != "undefined" ] && d="$d:$npmprefix/bin"
  fi
  printf '%s' "$d"
}

resolve_binary() {   # $1 = claude|codex; prints the resolved path, or nothing (rc 1)
  name="$1"
  p="$(command -v "$name" 2>/dev/null || true)"
  if [ -n "$p" ]; then printf '%s' "$p"; return 0; fi
  old_ifs="$IFS"; IFS=:
  for dir in $(extra_bin_dirs); do
    if [ -n "$dir" ] && [ -x "$dir/$name" ]; then
      IFS="$old_ifs"; printf '%s' "$dir/$name"; return 0
    fi
  done
  IFS="$old_ifs"
  return 1
}

host_bin() {   # $1 = claude|codex -> resolved binary path (must already be resolved)
  case "$1" in
    claude) printf '%s' "$CLAUDE_BIN" ;;
    codex) printf '%s' "$CODEX_BIN" ;;
  esac
}

report_config_only_hosts() {
  # A config home with no reachable binary: swarm can't be installed for it by this run, but it's
  # worth telling the user, since it means the host is (or was) in use here.
  ccd="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
  chd="${CODEX_HOME:-$HOME/.codex}"
  if [ -z "$CLAUDE_BIN" ] && [ -d "$ccd" ]; then
    warn "found a Claude config dir at $ccd but no 'claude' binary on PATH or in common install locations (~/.local/bin, npm global bin, ~/.claude/local, /usr/local/bin, /opt/homebrew/bin); skipping Claude -- put the CLI on PATH and re-run, or pass --host codex to silence this"
  fi
  if [ -z "$CODEX_BIN" ] && [ -d "$chd" ]; then
    warn "found a Codex config dir at $chd but no 'codex' binary on PATH or in common install locations; skipping Codex -- put the CLI on PATH and re-run, or pass --host claude to silence this"
  fi
}

detect_hosts() {
  # Sets the globals HOSTS, CLAUDE_BIN, CODEX_BIN directly: must NOT be run in a subshell (no
  # `x="$(detect_hosts)"`), or those assignments are lost when the subshell exits.
  case "$HOST_ARG" in
    claude|codex) want="$HOST_ARG" ;;
    both|"") want="claude codex" ;;
  esac
  found=""
  for name in $want; do
    if bin="$(resolve_binary "$name")"; then
      found="$found $name"
      case "$name" in
        claude) CLAUDE_BIN="$bin" ;;
        codex) CODEX_BIN="$bin" ;;
      esac
    elif [ -n "$HOST_ARG" ] && [ "$HOST_ARG" != "both" ]; then
      RUN_RESULT="no-cli"
      die "--host $HOST_ARG given, but no '$name' binary found on PATH or in common install locations"
    fi
  done
  HOSTS="$found"
}

# --------------------------------------------------------------------------- marketplace / plugin

# Which revision to install: --ref wins; channel main is the unpinned default branch; channel
# release is the newest vX.Y.Z tag (git ls-remote works anonymously on GitHub and Gitea), falling
# back to main with a warning when there is none. A local --marketplace path is a frozen tree:
# nothing to pin.
resolve_channel() {
  PIN_REF=""; PIN_ACTIVE=0
  if [ -d "$MARKETPLACE" ]; then
    log "channel: the marketplace is a local path ($MARKETPLACE): installing it as is"
    return 0
  fi
  PIN_ACTIVE=1
  if [ -n "$REF_GIVEN" ]; then
    PIN_REF="$REF_GIVEN"
    log "channel: ref $PIN_REF (explicit --ref)"
  elif [ "$CHANNEL" = "main" ]; then
    log "channel: main (tip of main)"
  else
    tag="$(GIT_TERMINAL_PROMPT=0 git ls-remote --tags --refs --sort=-v:refname "$MARKETPLACE" 'v*' 2>/dev/null </dev/null \
           | sed -n 's|.*refs/tags/\(v[0-9][0-9]*\.[0-9][0-9]*\.[0-9][0-9]*\)$|\1|p' | head -n1 || true)"
    if [ -n "$tag" ]; then
      PIN_REF="$tag"
      log "channel: release (newest tag $PIN_REF)"
    else
      warn "no release tag (vX.Y.Z) found at $MARKETPLACE, or git ls-remote failed: falling back to the tip of main"
      CHANNEL="main"
      log "channel: main (tip of main, fallback)"
    fi
  fi
}

pinned_source() {   # the marketplace source for claude: <url>#<ref> when pinned
  host="$1"
  if [ "$host" = "claude" ] && [ -n "$PIN_REF" ]; then printf '%s#%s' "$MARKETPLACE" "$PIN_REF"; else printf '%s' "$MARKETPLACE"; fi
}

marketplace_add_or_update() {
  host="$1"; bin="$(host_bin "$host")"
  src="$(pinned_source "$host")"
  set -- "$src"
  if [ "$host" = "codex" ] && [ -n "$PIN_REF" ]; then set -- "$src" --ref "$PIN_REF"; fi
  log "[$host] marketplace: add/update $src${PIN_REF:+ (ref $PIN_REF)}"
  if "$bin" plugin marketplace add "$@" >"$TMP_DIR/mp-add.$host.log" 2>&1 </dev/null; then
    log "[$host] marketplace added"
    return 0
  fi
  if [ "$PIN_ACTIVE" = "1" ]; then
    # Already registered, maybe at another ref: only remove+add moves it to the chosen one (a
    # plain update would stay on the old ref). Claude drops the plugin with its marketplace;
    # plugin_install puts it back.
    "$bin" plugin marketplace remove "$MARKETPLACE_NAME" >"$TMP_DIR/mp-remove.$host.log" 2>&1 </dev/null || true
    if "$bin" plugin marketplace add "$@" >"$TMP_DIR/mp-readd.$host.log" 2>&1 </dev/null; then
      log "[$host] marketplace re-added at ${PIN_REF:-the tip of main}"
      return 0
    fi
  fi
  # Codex has "marketplace upgrade" (same name argument), older CLIs "marketplace update"; try upgrade
  # first there, then the older name. Claude only has "update".
  if [ "$host" = "codex" ] \
     && "$bin" plugin marketplace upgrade "$MARKETPLACE_NAME" >"$TMP_DIR/mp-upgrade.$host.log" 2>&1 </dev/null; then
    log "[$host] marketplace already present; upgraded"
    return 0
  fi
  if "$bin" plugin marketplace update "$MARKETPLACE_NAME" >"$TMP_DIR/mp-update.$host.log" 2>&1 </dev/null; then
    log "[$host] marketplace already present; updated"
    return 0
  fi
  # Fall back to the remove+add pattern the README's rollout uses to switch marketplaces: covers
  # CLIs with no "marketplace update" subcommand, and a marketplace entry pointing somewhere else.
  "$bin" plugin marketplace remove "$MARKETPLACE_NAME" >"$TMP_DIR/mp-remove.$host.log" 2>&1 </dev/null || true
  if "$bin" plugin marketplace add "$@" >"$TMP_DIR/mp-readd.$host.log" 2>&1 </dev/null; then
    log "[$host] marketplace re-added (remove+add) after add/update failed"
    return 0
  fi
  cat "$TMP_DIR/mp-add.$host.log" "$TMP_DIR/mp-upgrade.$host.log" "$TMP_DIR/mp-update.$host.log" "$TMP_DIR/mp-readd.$host.log" >&2 2>/dev/null || true
  die "[$host] could not add or update the marketplace $MARKETPLACE (see output above)"
}

plugin_install() {
  host="$1"; bin="$(host_bin "$host")"
  case "$host" in
    claude) sub="install" ;;
    codex) sub="add" ;;
  esac
  log "[$host] plugin $sub $PLUGIN_SPEC"
  if ! "$bin" plugin "$sub" "$PLUGIN_SPEC" >"$TMP_DIR/plugin-$sub.$host.log" 2>&1 </dev/null; then
    # Idempotent: an already-installed plugin can make "install"/"add" itself fail or no-op
    # depending on CLI version. Only a real problem if the plugin then isn't listed.
    if ! "$bin" plugin list 2>/dev/null </dev/null | grep -q "$PLUGIN_SPEC"; then
      cat "$TMP_DIR/plugin-$sub.$host.log" >&2
      set_status "$host" installed "no"
      die "[$host] plugin $sub $PLUGIN_SPEC failed and it is not listed as installed (see output above)"
    fi
    log "[$host] plugin $PLUGIN_SPEC already installed"
  else
    log "[$host] plugin $PLUGIN_SPEC installed/updated"
  fi
  set_status "$host" installed "yes"
}

activate_host() {
  # "Activate" per host, within what each host actually supports non-interactively:
  #  - Claude: enable the plugin if it isn't already (best effort: older CLIs may install it
  #    already enabled and have no separate "enable" subcommand -- not fatal either way, `doctor`
  #    is the real check). Codex has no equivalent: its activation gate is hook trust, which is
  #    deliberately manual (see the header comment) and printed by codex_notes.
  host="$1"; bin="$(host_bin "$host")"
  case "$host" in
    claude)
      "$bin" plugin enable "$PLUGIN_SPEC" >"$TMP_DIR/enable.$host.log" 2>&1 </dev/null || true
      if "$bin" plugin list 2>/dev/null </dev/null | grep -q "$PLUGIN_SPEC"; then
        log "[$host] plugin enabled (doctor confirms hooks are actually active)"
        set_status "$host" enabled "yes"
      else
        warn "[$host] plugin $PLUGIN_SPEC not listed after enable attempt; check '$bin plugin list'"
        set_status "$host" enabled "no"
      fi
      ;;
    codex)
      log "[$host] activation needs the manual /hooks trust step (see the note printed at the end)"
      set_status "$host" enabled "manual(/hooks)"
      ;;
  esac
}

# --------------------------------------------------------------------------- locate swarm bin

newest_installed_plugin() {
  # The root of the swarm plugin this host's plugin manager just installed. Claude reports the
  # install path directly (installed_plugins.json installPaths for swarm@swarm): when any of
  # those paths has an executable bin/swarm, that's authoritative and a same- or higher-numbered
  # but *unreported* directory sitting in <config>/plugins/cache/swarm/swarm/<version> (a stale
  # tree from a previous install that wasn't cleaned up) is never preferred over it -- otherwise
  # a leftover 1.0.0 cache dir would keep outranking a freshly installed 0.1.0 forever. Only when
  # nothing is reported does the cache glob's highest manifest version, most recently modified on
  # a tie, decide. Codex has no installed_plugins.json, but `codex plugin list --json` reports the
  # installed version directly, so only the cache directory matching that version counts as
  # reported for Codex too; every other cache directory is a cache-only candidate, same as Claude.
  # Prints nothing (rc 1) when there is none.
  host="$1"
  python3 - "$host" "${CLAUDE_CONFIG_DIR:-$HOME/.claude}" "${CODEX_HOME:-$HOME/.codex}" \
      "$MARKETPLACE_NAME" "$PLUGIN_SPEC" <<'PY'
import glob, json, os, subprocess, sys
host, ccd, chd, mp, spec = sys.argv[1:6]
plugin = spec.split("@")[0]
reported = []
cache_only = []

def version(root):
    for m in (".claude-plugin/plugin.json", ".codex-plugin/plugin.json"):
        try:
            with open(os.path.join(root, m)) as f:
                return tuple(int(p) if p.isdigit() else 0 for p in str(json.load(f)["version"]).split("."))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return (0,)

def version_str(root):
    return ".".join(str(p) for p in version(root))

if host == "claude":
    try:
        with open(os.path.join(ccd, "plugins", "installed_plugins.json")) as f:
            data = json.load(f)
        for inst in (data.get("plugins") or {}).get(spec) or []:
            if isinstance(inst, dict) and inst.get("installPath"):
                reported.append(inst["installPath"])
    except (OSError, ValueError, AttributeError):
        pass
    cache_only = glob.glob(os.path.join(ccd, "plugins", "cache", mp, plugin, "*"))
elif host == "codex":
    cache_only = glob.glob(os.path.join(chd, "plugins", "cache", mp, plugin, "*"))
    installed_v = None
    try:
        res = subprocess.run(["codex", "plugin", "list", "--json"], capture_output=True, text=True, timeout=15)
        if res.returncode == 0:
            data = json.loads(res.stdout)
            for e in (data.get("installed") or []) if isinstance(data, dict) else []:
                pid = str(e.get("pluginId", "")) if isinstance(e, dict) else ""
                if (e.get("name") == "swarm" or pid.startswith("swarm@")) and e.get("installed") is True:
                    v = e.get("version")
                    if v:
                        installed_v = str(v)
                        break
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    if installed_v is not None:
        reported = [c for c in cache_only if version_str(c) == installed_v]
        cache_only = [c for c in cache_only if c not in reported]

def usable(cands):
    return [c for c in dict.fromkeys(cands) if os.access(os.path.join(c, "bin", "swarm"), os.X_OK)]

ok = usable(reported) or usable(cache_only)
if not ok:
    sys.exit(1)
print(max(ok, key=lambda r: (version(r), os.stat(r).st_mtime)))
PY
}

locate_swarm_bin() {
  # Always the freshly installed plugin's own bin/swarm, never ~/.local/bin/swarm: that launcher
  # may still point at another tree (an old frozen ~/src/swarm-release also saying 0.1.0), and
  # running bootstrap through it would leave the new plugin's code unused and the launcher
  # unrepointed. Bootstrap run from the new tree repoints the launcher at it.
  host="$1"
  if root="$(newest_installed_plugin "$host")"; then
    echo "$root/bin/swarm"; return 0
  fi
  # A plugin manager that runs a local --marketplace tree in place (source "./"), without a
  # cache copy: that tree is the plugin root.
  if [ -d "$MARKETPLACE" ] && [ -x "$MARKETPLACE/bin/swarm" ]; then
    echo "$MARKETPLACE/bin/swarm"; return 0
  fi
  return 1
}

# --------------------------------------------------------------------------- bootstrap / migrate / doctor

run_bootstrap() {
  host="$1"; sw="$2"
  log "[$host] swarm bootstrap --host $host"
  set +e
  # SWARM_NO_MIGRATE: bootstrap would otherwise also run migrate itself (spec's own idempotent
  # first-run step, for whoever calls bootstrap directly, e.g. the SessionStart hook). Called
  # from here that would run migrate once per host bootstrapped; this script runs it itself,
  # once per user, after every host's bootstrap (run_migrate, below) -- never both.
  # SWARM_CHANNEL: remember an explicitly chosen channel for `swarm upgrade` (bootstrap writes it
  # into the config's [upgrade] section).
  chan=""; if [ "$CHANNEL_GIVEN" = "1" ] && [ -z "$REF_GIVEN" ] && [ "$PIN_ACTIVE" = "1" ]; then chan="$CHANNEL"; fi
  out="$(SWARM_CHANNEL="$chan" SWARM_NO_MIGRATE=1 SWARM_APPLY_SETTINGS=1 "$sw" bootstrap --host "$host" 2>&1 </dev/null)"
  rc=$?
  set -e
  printf '%s\n' "$out" | paint_lines
  if [ "$rc" -ne 0 ]; then
    die "[$host] swarm bootstrap failed (see output above)"
  fi
  cfgline="$(printf '%s\n' "$out" | grep -E '^config ' || true)"
  if printf '%s' "$cfgline" | grep -q 'manual'; then
    CONFIG_NEEDS_ATTENTION=1
    CONFIG_DETAIL="$cfgline"
  fi
}

# The markers --force is about to override: each job name from migrate's refusal message, cross
# checked read-only against `swarm status --all` (never touches anything), so a forced run says
# plainly whether it is overriding a genuinely stale marker or a job that is still open.
warn_forced_migrate() {
  sw="$1"; refusal="$2"
  jobs="$(printf '%s' "$refusal" | sed -n 's/.*active on this machine (\(.*\)):.*/\1/p')"
  [ -n "$jobs" ] || return 0
  warn "--force: overriding stale local job marker(s): $jobs"
  board_ok=1
  board="$("$sw" status --all --no-color 2>/dev/null </dev/null)" || board_ok=0
  old_ifs="$IFS"; IFS=,
  set -f
  for j in $jobs; do
    IFS="$old_ifs"
    j="$(printf '%s' "$j" | sed 's/^ *//; s/ *$//')"
    [ -n "$j" ] || { IFS=,; continue; }
    if [ "$board_ok" != "1" ]; then
      warn "    $j: board unreachable: can't tell whether these jobs are open"
      IFS=,; continue
    fi
    st="$(printf '%s' "$board" | awk -v job="$j" '$1==job {print $2; exit}')"
    case "$st" in
      "")
        log "    $j: not on the board (stale marker; safe to override)" ;;
      completed|cancelled|failed)
        log "    $j: closed on the board (status: $st)" ;;
      *)
        warn "    $j: still $st on the board -- forcing migrate anyway runs alongside a job that may still be using the old hooks. Double check before continuing." ;;
    esac
    IFS=,
  done
  set +f
  IFS="$old_ifs"
}

run_migrate() {
  # No host: migrate is host-independent (settings.json, the skill dir, the board, the Codex
  # config are all per-user, not per-host), and runs once per user -- see current_user_main.
  sw="$1"
  log "swarm migrate"
  set +e
  out="$("$sw" migrate 2>&1 </dev/null)"
  rc=$?
  set -e
  printf '%s\n' "$out" | paint_lines
  if [ "$rc" -eq 0 ]; then return 0; fi
  if ! printf '%s' "$out" | grep -q 'active on this machine'; then
    die "swarm migrate failed (see output above)"
  fi
  if [ "$FORCE" != "1" ]; then
    RUN_RESULT="refused(active-job)"
    die "swarm migrate refused: a swarm job is active on this machine (see above). This installer does not force through it: wait for the job to finish, ask its owner, re-run with --force to override stale markers, or run 'swarm migrate --force' by hand."
  fi
  warn_forced_migrate "$sw" "$out"
  log "swarm migrate --force"
  set +e
  out2="$("$sw" migrate --force 2>&1 </dev/null)"
  rc2=$?
  set -e
  printf '%s\n' "$out2" | paint_lines
  [ "$rc2" -eq 0 ] || die "swarm migrate --force failed (see output above)"
}

run_doctor() {
  host="$1"; sw="$2"
  log "[$host] swarm doctor --host $host"
  set +e
  out="$("$sw" doctor --host "$host" 2>&1 </dev/null)"
  rc=$?
  set -e
  printf '%s\n' "$out" | paint_lines
  if [ "$rc" -ne 0 ]; then
    DOCTOR_FAILED=1
    warn "[$host] swarm doctor reported at least one FAIL (see above)"
    set_status "$host" doctor "FAIL"
  else
    set_status "$host" doctor "OK"
  fi
}

# --------------------------------------------------------------------------- per-user status
# bash 3.2: no associative arrays, so one plain variable per host (only ever two hosts).

set_status() {   # $1 = host, $2 = field, $3 = value ("field=value" appended)
  host="$1"; field="$2"; value="$3"
  case "$host" in
    claude) STATUS_CLAUDE="$STATUS_CLAUDE $field=$value" ;;
    codex) STATUS_CODEX="$STATUS_CODEX $field=$value" ;;
  esac
}

get_status() {
  case "$1" in
    claude) printf '%s' "$STATUS_CLAUDE" ;;
    codex) printf '%s' "$STATUS_CODEX" ;;
  esac
}

field_of() {   # $1 = status string, $2 = field name -> its value or "-"
  for kv in $1; do
    case "$kv" in
      "$2="*) printf '%s' "${kv#"$2"=}"; return 0 ;;
    esac
  done
  printf -- '-'
}

self_name() { id -un 2>/dev/null || echo "${USER:-?}"; }

print_summary() {
  self="$(self_name)"
  printf '\n%s%-10s %-8s %-32s %-12s %-12s %s%s\n' "$C_BOLD" "USER" "HOST" "CONFIG HOME" "INSTALLED" "ENABLED" "DOCTOR" "$C_RESET"
  for host in $HOSTS; do
    case "$host" in
      claude) home="${CLAUDE_CONFIG_DIR:-$HOME/.claude}" ;;
      codex) home="${CODEX_HOME:-$HOME/.codex}" ;;
    esac
    st="$(get_status "$host")"
    printf '%-10s %-8s %-32s %s %s %s\n' "$self" "$host" "$home" \
      "$(paint_word "$(field_of "$st" installed)" 12)" \
      "$(paint_word "$(field_of "$st" enabled)" 12)" \
      "$(paint_word "$(field_of "$st" doctor)")"
  done
}

emit_results() {
  # "swarm-install-result: <host> <status>", one line per host ("-" for no host at all). Status
  # words, no spaces: ok, ok(/hooks-pending), doctor-FAIL, refused(active-job),
  # needs-board-config, no-cli, failed.
  rc="$1"
  if [ -z "${HOSTS// /}" ]; then
    printf '%s %s %s\n' "$RESULT_PREFIX" "-" "${RUN_RESULT:-failed}"
    return 0
  fi
  for host in $HOSTS; do
    if [ -n "$RUN_RESULT" ]; then
      s="$RUN_RESULT"
    elif [ "$rc" -ne 0 ] && [ "$DOCTOR_FAILED" != "1" ]; then
      s="failed"
    else
      case "$(field_of "$(get_status "$host")" doctor)" in
        OK) s="ok"; [ "$host" = "codex" ] && s="ok(/hooks-pending)" ;;
        FAIL) s="doctor-FAIL" ;;
        *) s="failed" ;;
      esac
    fi
    printf '%s %s %s\n' "$RESULT_PREFIX" "$host" "$s"
  done
}

# --------------------------------------------------------------------------- other OS users

enumerate_users() {
  # "user:home" for every human user, one per line. SWARM_INSTALL_TEST_USERS (";" separated
  # "user:home") replaces the system's user list for tests, so this is exercisable against
  # scratch homes and never the real ones.
  if [ -n "${SWARM_INSTALL_TEST_USERS:-}" ]; then
    printf '%s\n' "$SWARM_INSTALL_TEST_USERS" | tr ';' '\n'
    return 0
  fi
  if [ "$(uname -s 2>/dev/null)" = "Darwin" ]; then
    for d in /Users/*; do
      [ -d "$d" ] || continue
      n="${d##*/}"
      case "$n" in Shared|Guest|.*) continue ;; esac
      id -u "$n" >/dev/null 2>&1 || continue
      printf '%s:%s\n' "$n" "$d"
    done
    return 0
  fi
  if command -v getent >/dev/null 2>&1; then
    getent passwd
  else
    cat /etc/passwd 2>/dev/null
  fi | awk -F: '$3 >= 1000 && $3 < 65534 && $7 != "" && $7 !~ /(nologin|false|sync|halt|shutdown)$/ {print $1":"$6}'
}

user_has_hosts() {   # $1 = home: has a claude/codex config home, or a binary in a per-user bin dir
  h="$1"
  [ -d "$h/.claude" ] || [ -d "$h/.codex" ] && return 0
  old_ifs="$IFS"; IFS=:
  for dir in $(user_bin_dirs "$h"); do
    if [ -x "$dir/claude" ] || [ -x "$dir/codex" ]; then IFS="$old_ifs"; return 0; fi
  done
  IFS="$old_ifs"
  return 1
}

candidate_users() {   # "user:home" lines for users other than $1 with a claude/codex install
  skip="$1"
  enumerate_users | while IFS=: read -r uname uhome; do
    [ -z "$uname" ] && continue
    [ "$uname" = "$skip" ] && continue
    [ -n "$uhome" ] && [ -d "$uhome" ] || continue
    user_has_hosts "$uhome" && printf '%s:%s\n' "$uname" "$uhome"
  done
  return 0
}

running_from_file() {   # prints the installer's own path if it runs from a file, not a pipe
  # Piped into bash, BASH_SOURCE[0] is "main" and $0 is "bash": a file that happens to be named
  # ./main must not pass for the installer, so the source must also be the script bash was
  # started with.
  src="${BASH_SOURCE[0]:-}"
  if [ -n "$src" ] && [ "$src" = "$0" ] && [ -f "$src" ] \
      && grep -q 'swarm one-shot installer' "$src" 2>/dev/null; then
    printf '%s' "$src"; return 0
  fi
  return 1
}

sudo_command_hint() {
  flags="$(host_flags_quoted)"
  if src="$(running_from_file)"; then
    printf 'sudo bash %s%s' "$(printf '%q' "$src")" "$flags"
  elif [ -n "$flags" ]; then
    printf 'curl -fsSL %s | sudo bash -s --%s' "$INSTALL_URL" "$flags"
  else
    printf 'curl -fsSL %s | sudo bash' "$INSTALL_URL"
  fi
}

note_other_users() {
  # Not root: this run covers the invoking user only, and never calls sudo/su. Say who else is
  # here and the one command that covers them too.
  self="$(self_name)"
  others="$(candidate_users "$self")"
  [ -n "$others" ] || return 0
  log "other OS users on this machine with claude/codex (NOT covered by this run, which only acts as $self):"
  printf '%s\n' "$others" | while IFS=: read -r uname uhome; do
    log "    $uname ($uhome)"
  done
  log "to install for every user on this machine, run as root:"
  log "    $(sudo_command_hint)"
}

# --------------------------------------------------------------------------- codex notes

codex_notes() {
  cat <<'EOF'

Codex: manual step required
----------------------------
Codex runs no plugin hook until its hooks are trusted, and it only reads ~/.codex/config.toml at
session start:

  1. Start an interactive `codex` session, run /hooks, and trust the swarm plugin's hooks.
  2. Start one more NEW Codex session (not the one you trusted from): that is the one whose
     SessionStart hook actually runs, and the one that picks up the config.toml changes
     `swarm bootstrap` already made (writable roots, agents.max_depth).
  3. From inside that new session (or a shell), check: swarm doctor --host codex
EOF
}

# --------------------------------------------------------------------------- all-users mode (root)

make_installer_copy() {
  # A root-owned copy every user can read: 0644 file in a 0755 mktemp dir under /tmp, removed by
  # the exit trap. From a file, a plain copy; from a pipe, rebuilt from the parsed functions.
  COPY_DIR="$(mktemp -d /tmp/swarm-install-root.XXXXXX)"
  chmod 0755 "$COPY_DIR"
  INSTALLER_COPY="$COPY_DIR/install.sh"
  if src="$(running_from_file)"; then
    cp "$src" "$INSTALLER_COPY"
  else
    {
      printf '#!/usr/bin/env bash\n# swarm one-shot installer (rebuilt by a root run from a piped install.sh)\n'
      printf 'set -euo pipefail\n'
      declare -f
      printf 'main "$@"\n'
    } > "$INSTALLER_COPY"
  fi
  chmod 0644 "$INSTALLER_COPY"
  bash -n "$INSTALLER_COPY" || die "internal error: the per-user copy of the installer at $INSTALLER_COPY doesn't parse"
}

run_as_user() {   # $1 = user, $2 = home; the installer copy's args follow
  u="$1"; h="$2"; shift 2
  # sudo -u drops XDG_RUNTIME_DIR, without which `systemctl --user` (the supervisor unit, for a
  # user with linger) can't reach the user's manager: hand it the user's own runtime dir, when
  # there is one.
  rt=""
  ruid="$(id -u "$u" 2>/dev/null || true)"
  rbase="${SWARM_INSTALL_TEST_RUN_USER_DIR:-/run/user}"
  [ -n "$ruid" ] && [ -d "$rbase/$ruid" ] && rt="$rbase/$ruid"
  if command -v sudo >/dev/null 2>&1; then
    if [ -n "$rt" ]; then
      (cd / && sudo -u "$u" -H env HOME="$h" XDG_RUNTIME_DIR="$rt" bash "$INSTALLER_COPY" "$@" </dev/null)
    else
      (cd / && sudo -u "$u" -H env HOME="$h" bash "$INSTALLER_COPY" "$@" </dev/null)
    fi
  elif command -v su >/dev/null 2>&1; then
    cmd="env HOME=$(printf '%q' "$h")"
    [ -n "$rt" ] && cmd="$cmd XDG_RUNTIME_DIR=$(printf '%q' "$rt")"
    cmd="$cmd bash $(printf '%q' "$INSTALLER_COPY")"
    for a in "$@"; do cmd="$cmd $(printf '%q' "$a")"; done
    (cd / && su - "$u" -c "$cmd" </dev/null)
  else
    echo "swarm-install: neither sudo nor su is available to run the installer as $u" >&2
    return 127
  fi
}

all_users_main() {
  log "$UPGRADE_NOTE"
  log "running as root: installing for every user on this machine with claude/codex, each run AS that user"

  users="$(candidate_users "root")"
  if [ -d "$HOME/.claude" ] || [ -d "$HOME/.codex" ]; then
    log "note: root's own $HOME has a claude/codex config home; all-users mode doesn't install for root. For root itself: bash install.sh --current-user (or: curl -fsSL $INSTALL_URL | bash -s -- --current-user)"
  fi
  if [ -z "$users" ]; then
    log "no human users with a ~/.claude or ~/.codex config home, or a claude/codex binary in their own bin dirs, found; nothing to do"
    return 0
  fi

  log "users found:"
  printf '%s\n' "$users" | while IFS=: read -r uname uhome; do log "    $uname ($uhome)"; done
  confirm "Install/update/activate swarm for each of these users, running as them, from $MARKETPLACE ?" \
    || die "aborted (not confirmed)"

  make_installer_copy
  log "per-user installer copy: $INSTALLER_COPY (removed on exit)"

  set --
  while IFS= read -r w; do [ -n "$w" ] && set -- "$@" "$w"; done <<EOF
$(host_flags)
EOF

  rows=""
  any_bad=0
  while IFS=: read -r uname uhome; do
    [ -z "$uname" ] && continue
    printf '\n'
    log "================ user $uname ($uhome) ================"
    ulog="$TMP_DIR/user.$uname.log"
    set +e
    run_as_user "$uname" "$uhome" --current-user --yes "$@" 2>&1 | tee "$ulog"
    rc="${PIPESTATUS[0]}"
    set -e
    got="$(grep "^$RESULT_PREFIX " "$ulog" 2>/dev/null || true)"
    if [ -z "$got" ]; then
      rows="$rows$uname - failed(rc=$rc)
"
      any_bad=1
      continue
    fi
    while read -r _p host status; do
      rows="$rows$uname $host $status
"
      case "$status" in
        ok|ok\(*|needs-board-config|no-cli) : ;;
        *) any_bad=1 ;;
      esac
    done <<EOF
$got
EOF
  done <<EOF
$users
EOF

  printf '\n%s%-16s %-8s %s%s\n' "$C_BOLD" "USER" "HOST" "STATUS" "$C_RESET"
  printf '%s' "$rows" | while read -r u h s; do
    [ -n "$u" ] && printf '%-16s %-8s %s\n' "$u" "$h" "$(paint_word "$s")"
  done

  codex_users="$(printf '%s' "$rows" | awk '$2 == "codex" && $3 !~ /^(failed|no-cli)/ {print $1}')"
  if [ -n "$codex_users" ]; then
    cat <<'EOF'

Codex: manual /hooks trust step, per user
------------------------------------------
Codex runs no plugin hook until its hooks are trusted; nothing can grant that on a user's behalf.
EOF
    for u in $codex_users; do
      cat <<EOF
  $u:
    1. sudo -iu $u          (or log in as $u)
    2. codex                -> run /hooks, trust the swarm plugin's hooks, then exit
    3. codex                -> a NEW session: its SessionStart hook runs, config.toml is re-read
    4. swarm doctor --host codex
EOF
    done
  fi

  if printf '%s' "$rows" | grep -q ' needs-board-config$'; then
    printf '\n'
    log "users marked needs-board-config: fill in their ~/.config/swarm/config.toml (the output above says what), then re-run this installer."
  fi

  log "$UPGRADE_NOTE"
  [ "$any_bad" = "0" ] || die "at least one user's install failed or was refused (see the table above); the others were installed. Fix it, then re-run (idempotent)."
  log "done."
}

# --------------------------------------------------------------------------- current-user mode

current_user_main() {
  log "$UPGRADE_NOTE"

  detect_hosts
  report_config_only_hosts
  if [ "$CURRENT_USER_ONLY" != "1" ]; then note_other_users; fi
  if [ -z "${HOSTS// /}" ]; then
    RUN_RESULT="no-cli"
    die "no claude or codex CLI found on PATH or in common install locations, and --host wasn't given; install one of them first, or pass --host claude|codex|both"
  fi
  log "hosts: $HOSTS"
  log "marketplace: $MARKETPLACE"

  preflight
  resolve_channel

  confirm "Install/update/activate swarm for:$HOSTS from $MARKETPLACE ?" || die "aborted (not confirmed)"

  for host in $HOSTS; do
    marketplace_add_or_update "$host"
    plugin_install "$host"
    activate_host "$host"
  done

  for host in $HOSTS; do
    sw="$(locate_swarm_bin "$host")" || die "[$host] can't find the installed swarm plugin's bin/swarm in $host's plugin cache; check '$(host_bin "$host") plugin list', then re-run install.sh"
    log "[$host] using $sw"
    pv="$(sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$(dirname "$(dirname "$sw")")/.claude-plugin/plugin.json" "$(dirname "$(dirname "$sw")")/.codex-plugin/plugin.json" 2>/dev/null | head -n1 || true)"
    log "[$host] installed swarm ${pv:-unknown}: channel $CHANNEL, ref ${PIN_REF:-main (tip)}"
    run_bootstrap "$host" "$sw"
  done

  if [ "$CONFIG_NEEDS_ATTENTION" = "1" ]; then
    log "board config needs your attention before migrate/doctor can do anything useful:"
    printf '%s\n' "$CONFIG_DETAIL"
    log "Nothing was invented: fix ~/.config/swarm/config.toml (or \$SWARM_CONFIG) yourself -- a"
    log "shared Postgres board's credentials are your step, never this script's -- then re-run"
    log "install.sh (idempotent) to run migrate and doctor."
    RUN_RESULT="needs-board-config"
    for host in $HOSTS; do
      set_status "$host" doctor "skipped(config)"
      if [ "$host" = "codex" ]; then codex_notes; fi
    done
    print_summary
    exit 0
  fi

  # migrate once per user, not once per host: it retires the one old ~/.claude/skills/swarm
  # install, board and spool -- none of that is host-specific, so a second attempt right after
  # the first (once for claude, once for codex) is pure redundancy (and a second refusal, if the
  # first one refused). Any host's freshly bootstrapped bin/swarm runs the same migrate.
  set -- $HOSTS
  migrate_host="$1"
  sw="$(locate_swarm_bin "$migrate_host")" || die "[$migrate_host] can't find swarm after bootstrap; this shouldn't happen"
  run_migrate "$sw"

  for host in $HOSTS; do
    sw="$(locate_swarm_bin "$host")" || die "[$host] can't find swarm after bootstrap; this shouldn't happen"
    run_doctor "$host" "$sw"
  done

  for host in $HOSTS; do
    if [ "$host" = "codex" ]; then codex_notes; fi
  done

  log "$UPGRADE_NOTE"
  print_summary

  if [ "$DOCTOR_FAILED" = "1" ]; then
    die "swarm doctor reported at least one FAIL above; fix it, then re-run install.sh (idempotent)"
  fi

  log "done."
}

# --------------------------------------------------------------------------- main

main() {
  init_globals
  parse_args "$@"
  setup_color
  trap on_exit EXIT
  trap 'exit 130' INT TERM

  is_root=0
  [ "$(id -u)" = "0" ] && is_root=1
  if [ "$ALL_USERS" = "1" ] && [ "$is_root" != "1" ]; then
    die "--all-users installs for every user on this machine and needs root; this script never runs sudo itself. Run: $(sudo_command_hint)"
  fi

  make_tmp_dir "$is_root"
  if [ "$is_root" = "1" ] && [ "$CURRENT_USER_ONLY" != "1" ]; then
    all_users_main
  else
    current_user_main
  fi
}

main "$@"
