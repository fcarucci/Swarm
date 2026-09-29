#!/bin/sh
# End to end tests for install.sh, entirely inside scratch HOMEs with stub claude/codex CLIs
# (record their args, never real plugin managers) and this checkout as a local --marketplace
# frozen tree. SWARM_VENV points at the checkout's already-built venv so bin/swarm needs no pip
# install (offline, fast). Never touches the real HOME, ~/.claude, ~/.codex, or the production swarm
# skill/board: every HOME below is a fresh mktemp directory.
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
# The venv bin/swarm runs from (the wrapper tests pass a throwaway one; CI has no checkout .venv).
E2E_VENV="${E2E_VENV:-$ROOT/.venv}"
INSTALL_SH="$ROOT/install.sh"
[ -f "$INSTALL_SH" ] || { echo "E2E FAIL: no install.sh at $INSTALL_SH" >&2; exit 1; }
[ -x "$ROOT/bin/swarm" ] || { echo "E2E FAIL: no $ROOT/bin/swarm (this checkout is the --marketplace frozen tree)" >&2; exit 1; }

fail() { echo "E2E FAIL: $*" >&2; exit 1; }

# --------------------------------------------------------------------------- portable helpers
# GNU tools (setsid, stat -c, sha256sum) aren't on macOS; fall back to what it does have (a
# python3 stdlib helper for setsid and file mode, shasum -a 256 for hashing) instead of skipping
# the checks these back.

_no_tty() {
  # Run "$@" detached from the controlling terminal, so /dev/tty can't be opened.
  if command -v setsid >/dev/null 2>&1; then
    setsid "$@"
  else
    python3 -c 'import os, sys
os.setsid()
os.execvp(sys.argv[1], sys.argv[1:])' "$@"
  fi
}

_mode_octal() {
  # $1's permission bits as octal digits (e.g. "644"), GNU or BSD/macOS stat.
  m="$(stat -c %a "$1" 2>/dev/null)" && { printf '%s\n' "$m"; return 0; }
  stat -f %A "$1"
}

_hash_files() {
  # sha256 of each path read from stdin (one per line, blanks skipped), GNU sha256sum or macOS
  # shasum -a 256.
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    if command -v sha256sum >/dev/null 2>&1; then
      sha256sum "$f"
    elif command -v shasum >/dev/null 2>&1; then
      shasum -a 256 "$f"
    else
      fail "no sha256sum or shasum on PATH to hash $f"
    fi
  done
}

SCRATCH_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/swarm-install-test.XXXXXX")"
cleanup() { rm -rf "$SCRATCH_ROOT"; }
trap cleanup EXIT INT TERM

# Every scenario runs install.sh under a stripped PATH (env -i, only the stub CLIs and the system
# bin dirs), which on macOS resolves python3 to Apple's 3.9 (install.sh needs 3.11+ for tomllib).
# So the interpreter this script itself runs under goes first on that PATH, as a one-entry dir.
PYBIN="$SCRATCH_ROOT/pybin"
mkdir -p "$PYBIN"
command -v python3 >/dev/null 2>&1 || fail "no python3 on PATH"
ln -s "$(command -v python3)" "$PYBIN/python3"

# ------------------------------------------------------------------------- stub claude/codex

# $1 = bin dir to write into; $2 = log dir; writes claude and codex stubs that record every
# invocation and behave like a real plugin manager just enough for install.sh: "add" succeeds
# once then fails (so a second run must fall through to "update"), "install"/"add" (plugin) and
# "enable" succeed, "list" reports installed once the install call has happened.
write_stub_clis() {
  # $3 = HOME of the scratch env (the stubs need it to write installed_plugins.json where the
  # real `swarm doctor` claude check reads it from, so these tests exercise doctor for real
  # instead of always reporting an unavoidable FAIL/unknown for a plugin manager that was never
  # really run).
  bindir="$1"; logdir="$2"; home="$3"
  mkdir -p "$bindir" "$logdir"
  cat > "$bindir/claude" <<STUB
#!/bin/sh
echo "\$@" >> "$logdir/claude.args"
case "\$1 \$2 \$3" in
  "plugin marketplace add")
    if [ -f "$logdir/claude.marketplace" ]; then exit 1; fi
    touch "$logdir/claude.marketplace"; exit 0 ;;
  "plugin marketplace update")
    [ -f "$logdir/claude.marketplace" ] && exit 0 || exit 1 ;;
  "plugin marketplace remove")
    rm -f "$logdir/claude.marketplace"; exit 0 ;;
esac
case "\$1 \$2" in
  "plugin install")
    touch "$logdir/claude.installed"
    mkdir -p "$home/.claude/plugins"
    printf '{"plugins": {"swarm@swarm": [{"installPath": "$ROOT"}]}}' > "$home/.claude/plugins/installed_plugins.json"
    exit 0 ;;
  "plugin enable") exit 0 ;;
  "plugin list")
    [ -f "$logdir/claude.installed" ] && echo "swarm@swarm  enabled (scope: user)"
    exit 0 ;;
esac
case "\$1" in
  --version) echo "stub-claude 9.9.9"; exit 0 ;;
esac
exit 0
STUB
  chmod +x "$bindir/claude"
  cat > "$bindir/codex" <<STUB
#!/bin/sh
echo "\$@" >> "$logdir/codex.args"
case "\$1 \$2 \$3" in
  "plugin marketplace add")
    if [ -f "$logdir/codex.marketplace" ]; then exit 1; fi
    touch "$logdir/codex.marketplace"; exit 0 ;;
  "plugin marketplace update")
    [ -f "$logdir/codex.marketplace" ] && exit 0 || exit 1 ;;
  "plugin marketplace remove")
    rm -f "$logdir/codex.marketplace"; exit 0 ;;
esac
case "\$1 \$2" in
  "plugin add")
    touch "$logdir/codex.installed"
    mkdir -p "$home/.codex/plugins/cache/swarm/swarm"
    ln -sfn "$ROOT" "$home/.codex/plugins/cache/swarm/swarm/local"
    exit 0 ;;
  "plugin list")
    if [ -f "$logdir/codex.installed" ]; then
      case "\$3" in
        --json) printf '{"installed": [{"pluginId": "swarm@swarm", "name": "swarm", "installed": true, "enabled": true}], "available": []}' ;;
        *) echo "PLUGIN        STATUS    VERSION  SOURCE" ; echo "swarm@swarm   enabled   0.1.0    swarm" ;;
      esac
    elif [ "\$3" = "--json" ]; then
      printf '{"installed": [], "available": []}'
    fi
    exit 0 ;;
esac
case "\$1" in
  --version) echo "stub-codex 9.9.9"; exit 0 ;;
esac
exit 0
STUB
  chmod +x "$bindir/codex"
}

# $1 = scratch home dir: a sqlite board config so bootstrap/migrate/doctor have something real
# and offline to work against (no board config at all is its own scenario below).
write_sqlite_config() {
  home="$1"
  mkdir -p "$home/.config/swarm"
  cat > "$home/.config/swarm/config.toml" <<EOF
[board]
backend = "sqlite"
spool_dir = "$home/.local/share/swarm-spool"
[sqlite]
path = "$home/.local/share/swarm-board/board.sqlite3"
[hook]
marker_dir = "$home/.local/state/swarm/active"
[transcripts]
enabled = false
[models]
mode = "default"
EOF
  chmod 600 "$home/.config/swarm/config.toml"
}

# One fresh scratch HOME with stub CLIs on PATH; nothing of the real HOME leaks in.
new_env() {
  n="$1"
  home="$SCRATCH_ROOT/$n/home"
  bindir="$SCRATCH_ROOT/$n/bin"
  logdir="$SCRATCH_ROOT/$n/log"
  mkdir -p "$home" "$bindir" "$logdir"
  write_stub_clis "$bindir" "$logdir" "$home"
  echo "$home:$bindir:$logdir"
}

run_install() {   # $1 = home, $2 = bindir, then install.sh args
  home="$1"; bindir="$2"; shift 2
  env -i HOME="$home" PATH="$bindir:$PYBIN:/usr/bin:/bin:/usr/local/bin" \
      SWARM_VENV="$E2E_VENV" SWARM_NO_MIGRATE=1 SWARM_NO_SYSTEMD=1 SWARM_AUTO_INIT=1 \
      TERM="${TERM:-dumb}" \
      ${SWARM_INSTALL_TEST_USERS:+SWARM_INSTALL_TEST_USERS="$SWARM_INSTALL_TEST_USERS"} \
      bash "$INSTALL_SH" --marketplace "$ROOT" "$@"
}

# --------------------------------------------------------------------------- test: --help

t_help() {
  # --help must execute nothing else: a real `swarm` (or anything else) on PATH must never be
  # invoked, and the usage heredoc's contents must never be interpreted as shell (see the
  # backtick-in-unquoted-heredoc bug this guards against). Run with a clean PATH pointing only at
  # a fake `swarm` that fails loudly if called, plus the minimum standard bin dirs install.sh's
  # own use of cat/sed/printf/grep needs.
  bindir="$SCRATCH_ROOT/help-path"; mkdir -p "$bindir"
  cat > "$bindir/swarm" <<'STUB'
#!/bin/sh
echo "FAKE SWARM CALLED: $*" >&2
exit 99
STUB
  chmod +x "$bindir/swarm"
  out="$(env -i PATH="$bindir:$PYBIN:/usr/bin:/bin:/usr/local/bin" HOME="$HOME" bash "$INSTALL_SH" --help)" \
    || fail "--help exited non-zero"
  echo "$out" | grep -q -- "--marketplace" || fail "--help doesn't document --marketplace"
  echo "$out" | grep -q -- "--all-users" || fail "--help doesn't document --all-users"
  echo "$out" | grep -qi "schema v9" || fail "--help doesn't carry the schema v9 upgrade-together note"
  echo "$out" | grep -q "FAKE SWARM CALLED" && fail "--help executed something on PATH: $out"
  echo "help: ok"
}

# --------------------------------------------------------------------------- test: no TTY

t_no_tty() {
  ie="$(new_env no_tty)"; home="${ie%%:*}"; rest="${ie#*:}"; bindir="${rest%%:*}"; logdir="${rest##*:}"
  write_sqlite_config "$home"
  # setsid detaches the controlling terminal, so /dev/tty can't be opened: this must fail with a
  # clear message and touch nothing (no marketplace/plugin calls), never guess "yes".
  set +e
  out="$(_no_tty env -i HOME="$home" PATH="$bindir:$PYBIN:/usr/bin:/bin:/usr/local/bin" \
      SWARM_VENV="$E2E_VENV" SWARM_NO_MIGRATE=1 SWARM_NO_SYSTEMD=1 \
      bash "$INSTALL_SH" --marketplace "$ROOT" --host claude < /dev/null 2>&1)"
  rc=$?
  set -e
  [ "$rc" -ne 0 ] || fail "no-TTY run exited 0, expected non-zero"
  echo "$out" | grep -qi "tty" || fail "no-TTY run didn't explain itself (no 'TTY' in output): $out"
  # preflight may harmlessly run `claude --version` (read-only); it must never get as far as
  # touching the marketplace or installing anything without a confirmed prompt.
  if [ -f "$logdir/claude.args" ]; then
    grep -q "^plugin " "$logdir/claude.args" && fail "no-TTY run went ahead and called a plugin subcommand; it must stop at the confirm prompt"
  fi
  echo "no-tty: ok"
}

# --------------------------------------------------------------------------- test: detection

t_detection() {
  ie="$(new_env detect)"; home="${ie%%:*}"; rest="${ie#*:}"; bindir="${rest%%:*}"; logdir="${rest##*:}"
  write_sqlite_config "$home"
  rm -f "$bindir/codex"   # only claude on PATH: codex must be skipped, not attempted
  out="$(run_install "$home" "$bindir" --yes)" || fail "detection run failed: $out"
  echo "$out" | grep -Eq '^==> hosts: *claude *$' || fail "hosts line doesn't say just 'claude': $out"
  [ -f "$logdir/claude.args" ] || fail "claude stub was never called"
  [ -f "$logdir/codex.args" ] && fail "codex stub was called despite not being on PATH"
  echo "detection: ok"
}

# --------------------------------------------------------------------------- test: idempotent re-run

t_idempotent() {
  ie="$(new_env idem)"; home="${ie%%:*}"; rest="${ie#*:}"; bindir="${rest%%:*}"; logdir="${rest##*:}"
  write_sqlite_config "$home"
  out1="$(run_install "$home" "$bindir" --host both --yes)" || fail "first run failed: $out1"
  echo "$out1" | grep -q "^==> done\.$" || fail "first run didn't finish cleanly: $out1"
  grep -q "^plugin marketplace add" "$logdir/claude.args" || fail "first run didn't try marketplace add for claude"

  # snapshot every file the first run could have touched or backed up, plus its hash
  before="$(find "$home/.claude" "$home/.codex" "$home/.config/swarm" -type f 2>/dev/null | sort)"
  [ -n "$before" ] || fail "no files found under $home/.claude, .codex or .config/swarm after the first run"
  before_hashes="$(printf '%s\n' "$before" | _hash_files)"
  [ -n "$before_hashes" ] || fail "hashing the first run's files produced nothing"
  before_backups="$(printf '%s\n' "$before" | grep -c '\.pre-swarm-' || true)"

  out2="$(run_install "$home" "$bindir" --host both --yes)" || fail "second (idempotent) run failed: $out2"
  echo "$out2" | grep -q "^==> done\.$" || fail "second run didn't finish cleanly: $out2"
  # second run's "add" fails in the stub (already added), so it must have fallen through to update
  grep -q "^plugin marketplace update" "$logdir/claude.args" || fail "second run didn't fall back to marketplace update"
  grep -q "^plugin marketplace update" "$logdir/codex.args" || fail "second run (codex) didn't fall back to marketplace update"

  # no "changed" status word anywhere in the second run's output (bootstrap/migrate/doctor lines)
  echo "$out2" | grep -qw "changed" && fail "second run printed a 'changed' line, not idempotent: $out2"
  echo "$out2" | grep -qi "launcher.*changed\|changed.*launcher" && fail "second run says the launcher changed: $out2"

  after="$(find "$home/.claude" "$home/.codex" "$home/.config/swarm" -type f 2>/dev/null | sort)"
  after_hashes="$(printf '%s\n' "$after" | _hash_files)"
  [ -n "$after_hashes" ] || fail "hashing the second run's files produced nothing"
  after_backups="$(printf '%s\n' "$after" | grep -c '\.pre-swarm-' || true)"
  [ "$after_backups" -eq "$before_backups" ] || fail "second run created new backup file(s): before=$before_backups after=$after_backups; files: $after"
  [ "$before_hashes" = "$after_hashes" ] || fail "second run changed file contents (hashes differ):\nbefore:\n$before_hashes\nafter:\n$after_hashes"
  echo "idempotent: ok"
}

# --------------------------------------------------------------------------- test: refuses while a job is active

t_refuses_active_job() {
  ie="$(new_env active)"; home="${ie%%:*}"; rest="${ie#*:}"; bindir="${rest%%:*}"; logdir="${rest##*:}"
  write_sqlite_config "$home"
  mkdir -p "$home/.local/state/swarm/active"
  printf '{"job": "recall-latency-2026-09"}' > "$home/.local/state/swarm/active/testjob.json"
  set +e
  out="$(run_install "$home" "$bindir" --host claude --yes 2>&1)"
  rc=$?
  set -e
  [ "$rc" -ne 0 ] || fail "run with an active job marker exited 0, expected non-zero"
  echo "$out" | grep -q "recall-latency-2026-09" || fail "refusal didn't name the active job: $out"
  echo "$out" | grep -qi "active" || fail "refusal didn't say a job is active: $out"
  echo "$out" | grep -q "swarm doctor" && fail "doctor ran despite the migrate refusal; it must stop first"
  echo "refuses-active-job: ok"
}

# --------------------------------------------------------------------------- test: --force overrides a stale marker

t_force_overrides_active_job() {
  ie="$(new_env forced)"; home="${ie%%:*}"; rest="${ie#*:}"; bindir="${rest%%:*}"; logdir="${rest##*:}"
  write_sqlite_config "$home"
  mkdir -p "$home/.local/state/swarm/active"
  printf '{"job": "recall-latency-2026-09"}' > "$home/.local/state/swarm/active/testjob.json"
  out="$(run_install "$home" "$bindir" --host claude --yes --force)" || fail "--force run failed: $out"
  echo "$out" | grep -q "^==> done\.$" || fail "--force run didn't finish cleanly: $out"
  echo "$out" | grep -q "recall-latency-2026-09" || fail "--force run didn't name the overridden marker: $out"
  echo "$out" | grep -qi "stale marker" || fail "--force run didn't say it's overriding a stale marker: $out"
  # the job isn't on this scratch board at all, so it must be reported as stale/safe, not a warning
  echo "$out" | grep -qi "still .* on the board" && fail "--force run warned about a job that isn't even on the board: $out"
  echo "$out" | grep -q "swarm doctor" || fail "--force run didn't get past migrate to doctor: $out"
  echo "force-overrides-active-job: ok"
}

# --------------------------------------------------------------------------- test: no board config -> file board, carries on

t_no_config_file_board() {
  ie="$(new_env noconfig)"; home="${ie%%:*}"; rest="${ie#*:}"; bindir="${rest%%:*}"; logdir="${rest##*:}"
  # deliberately no write_sqlite_config: bootstrap writes a config with the default file board,
  # which needs no server and no credentials, so the install just works (nothing is invented).
  out="$(run_install "$home" "$bindir" --host claude --yes)" || fail "no-config run failed: $out"
  echo "$out" | grep -q "^==> done\.$" || fail "no-config run didn't finish cleanly: $out"
  echo "$out" | grep -qi "fill in" && fail "asked the user to fill in a board config: $out"
  echo "$out" | grep -qE "PGPASSWORD=[^.]" && fail "must never print a credential value"
  echo "$out" | grep -q "swarm doctor" || fail "doctor didn't run on the file board: $out"
  [ -f "$home/.config/swarm/config.toml" ] || fail "bootstrap should still have created the config from the example"
  grep -Eq '^backend = "file"' "$home/.config/swarm/config.toml" || fail "the created config doesn't set backend = \"file\""
  echo "no-config-file-board: ok"
}

# --------------------------------------------------------------------------- stubs that must never run

# sudo/su stubs that blow up loudly: a non-root run must never call either.
write_forbidden_escalation() {
  bindir="$1"
  cat > "$bindir/sudo" <<'STUB'
#!/bin/sh
echo "SUDO WAS CALLED: $*" >&2
exit 99
STUB
  cat > "$bindir/su" <<'STUB'
#!/bin/sh
echo "SU WAS CALLED: $*" >&2
exit 99
STUB
  chmod +x "$bindir/sudo" "$bindir/su"
}

# --------------------------------------------------------------------------- test: not root -> note + sudo command, never escalates

t_non_root_notes_other_users() {
  ie="$(new_env nonroot)"; home="${ie%%:*}"; rest="${ie#*:}"; bindir="${rest%%:*}"; logdir="${rest##*:}"
  write_sqlite_config "$home"
  other_home="$SCRATCH_ROOT/nonroot/other_home"
  mkdir -p "$other_home/.codex"
  write_forbidden_escalation "$bindir"
  before="$(find "$other_home" | sort)"
  out="$(SWARM_INSTALL_TEST_USERS="otheruser:$other_home" run_install "$home" "$bindir" --host claude --yes 2>&1)" \
    || fail "non-root run failed: $out"
  echo "$out" | grep -q "otheruser ($other_home)" || fail "non-root run didn't list the other user it saw: $out"
  echo "$out" | grep -q "sudo bash .*install.sh --host claude --marketplace" \
    || fail "non-root run didn't print the exact sudo command covering the other users: $out"
  echo "$out" | grep -q "SUDO WAS CALLED" && fail "sudo was invoked by a non-root run: $out"
  echo "$out" | grep -q "SU WAS CALLED" && fail "su was invoked by a non-root run: $out"
  [ "$before" = "$(find "$other_home" | sort)" ] || fail "a non-root run wrote into the other user's home"
  echo "$out" | grep -q "^==> done\.$" || fail "non-root run didn't finish its own install: $out"
  echo "non-root-notes-other-users: ok"
}

# --------------------------------------------------------------------------- test: --all-users needs root

t_all_users_needs_root() {
  ie="$(new_env allneedsroot)"; home="${ie%%:*}"; rest="${ie#*:}"; bindir="${rest%%:*}"; logdir="${rest##*:}"
  write_sqlite_config "$home"
  write_forbidden_escalation "$bindir"
  set +e
  out="$(run_install "$home" "$bindir" --yes --all-users 2>&1)"
  rc=$?
  set -e
  [ "$rc" -ne 0 ] || fail "--all-users without root exited 0: $out"
  echo "$out" | grep -q "needs root" || fail "--all-users without root didn't say it needs root: $out"
  echo "$out" | grep -q "sudo bash" || fail "--all-users without root didn't print the sudo command: $out"
  echo "$out" | grep -q "SUDO WAS CALLED" && fail "sudo was invoked: $out"
  if [ -f "$logdir/claude.args" ]; then
    grep -q "^plugin " "$logdir/claude.args" && fail "--all-users without root still installed something"
  fi
  echo "all-users-needs-root: ok"
}

# --------------------------------------------------------------------------- simulated root

# A fake root: a stub `id` answering uid 0/"root" unless FAKE_UID/FAKE_USER say otherwise, and a
# stub `sudo` that runs the per-user command "as" the user by giving it that user's scratch HOME,
# stub CLIs and FAKE_UID. Nothing ever gets real root, and every home is a scratch dir. The sudo
# stub also checks what root handed over (copy perms, the user's own HOME) and snapshots each
# user's home right before and right after that user's run, so the test can prove root itself
# wrote nothing into it.
setup_root_env() {   # $1 = scenario name; prints its base dir
  base="$SCRATCH_ROOT/$1"
  mkdir -p "$base/roothome" "$base/rootbin" "$base/idbin" "$base/users" "$base/snap"
  : > "$base/uids"
  : > "$base/uids"
  cat > "$base/idbin/id" <<STUB
#!/bin/sh
BASE="$base"
STUB
  cat >> "$base/idbin/id" <<'STUB'
case "$*" in
  -u) echo "${FAKE_UID:-0}"; exit 0 ;;
  -un) echo "${FAKE_USER:-root}"; exit 0 ;;
  "-u "*)
    uid="$(awk -v n="$2" '$1 == n {print $2}' "$BASE/uids" 2>/dev/null)"
    [ -n "$uid" ] && { echo "$uid"; exit 0; }
    echo "id: '$2': no such user" >&2; exit 1 ;;
esac
exec /usr/bin/id "$@"
STUB
  cat > "$base/rootbin/sudo" <<STUB
#!/bin/sh
BASE="$base"
PYBIN="$PYBIN"
STUB
  cat >> "$base/rootbin/sudo" <<'STUB'
[ "$1" = "-u" ] || { echo "stub sudo: unexpected args: $*" >&2; exit 98; }
user="$2"; shift 2
[ "$1" = "-H" ] || { echo "stub sudo: expected -H: $*" >&2; exit 98; }
shift
[ "$1" = "env" ] || { echo "stub sudo: expected env HOME=...: $*" >&2; exit 98; }
home=""; xdg=""; copy=""; seen=0
for a in "$@"; do
  if [ "$seen" = 1 ]; then copy="$a"; break; fi
  case "$a" in
    HOME=*) home="${a#HOME=}" ;;
    XDG_RUNTIME_DIR=*) xdg="${a#XDG_RUNTIME_DIR=}" ;;
    bash) seen=1 ;;
  esac
done
[ -n "$home" ] && [ -n "$copy" ] || { echo "stub sudo: expected env HOME=... [XDG_RUNTIME_DIR=...] bash <copy>: $*" >&2; exit 98; }
echo "$user $*" >> "$BASE/sudo.log"
echo "$copy" >> "$BASE/copies"
[ "$home" = "$BASE/users/$user/home" ] || echo "$user: HOME=$home is not this user's home" >> "$BASE/violations"
copy_mode="$(stat -c %a "$copy" 2>/dev/null || stat -f %A "$copy")"
[ "$copy_mode" = "644" ] || echo "$user: installer copy $copy is not 0644" >> "$BASE/violations"
copydir_mode="$(stat -c %a "$(dirname "$copy")" 2>/dev/null || stat -f %A "$(dirname "$copy")")"
[ "$copydir_mode" = "755" ] || echo "$user: installer copy dir is not 0755" >> "$BASE/violations"
case "$copy" in "$BASE"/users/*) echo "$user: installer copy is inside a user home" >> "$BASE/violations" ;; esac
head -n 2 "$copy" > "$BASE/copy.head"
find "$home" | sort > "$BASE/snap/$user.handover"
cmp -s "$BASE/snap/$user.before" "$BASE/snap/$user.handover" \
  || echo "$user: root wrote into $home before handing over to $user" >> "$BASE/violations"
env FAKE_UID="$(awk -v n="$user" '$1 == n {print $2}' "$BASE/uids")" FAKE_USER="$user" \
    PATH="$BASE/users/$user/bin:$BASE/idbin:$PYBIN:/usr/bin:/bin:/usr/local/bin" "$@"
rc=$?
find "$home" | sort > "$BASE/snap/$user.after"
exit $rc
STUB
  chmod +x "$base/idbin/id" "$base/rootbin/sudo"
  echo "$base"
}

new_user() {   # $1 = base, $2 = user name; scratch home + that user's own stub CLIs
  base="$1"; u="$2"
  mkdir -p "$base/users/$u/home"
  n="$(( $(wc -l < "$base/uids" 2>/dev/null || echo 0) + 1001 ))"
  echo "$u $n" >> "$base/uids"
  write_stub_clis "$base/users/$u/bin" "$base/users/$u/log" "$base/users/$u/home"
}

snapshot_before() {   # $1 = base, then user names
  base="$1"; shift
  for u in "$@"; do find "$base/users/$u/home" | sort > "$base/snap/$u.before"; done
}

run_as_fake_root() {   # $1 = base, $2 = SWARM_INSTALL_TEST_USERS, then install.sh args
  base="$1"; users="$2"; shift 2
  env -i HOME="$base/roothome" PATH="$base/rootbin:$base/idbin:$PYBIN:/usr/bin:/bin:/usr/local/bin" \
      SWARM_VENV="$E2E_VENV" SWARM_NO_MIGRATE=1 SWARM_NO_SYSTEMD=1 SWARM_AUTO_INIT=1 \
      TERM="${TERM:-dumb}" SWARM_INSTALL_TEST_USERS="$users" \
      SWARM_INSTALL_TEST_RUN_USER_DIR="$base/run/user" \
      bash "$INSTALL_SH" --marketplace "$ROOT" "$@"
}

assert_root_wrote_nothing() {   # $1 = base, then the users that got a run
  base="$1"; shift
  if [ -f "$base/violations" ]; then fail "root-side violations: $(cat "$base/violations")"; fi
  for u in "$@"; do
    [ -f "$base/snap/$u.after" ] || fail "$u never got a run of its own"
    find "$base/users/$u/home" | sort > "$base/snap/$u.final"
    cmp -s "$base/snap/$u.after" "$base/snap/$u.final" \
      || fail "root wrote into $u's home after $u's own run: $(diff "$base/snap/$u.after" "$base/snap/$u.final")"
  done
  while read -r c; do
    [ -e "$c" ] && fail "installer copy $c was left behind"
  done < "$base/copies"
  return 0
}

# --------------------------------------------------------------------------- test: root -> every user, as that user

t_root_all_users() {
  base="$(setup_root_env rootall)"
  # alice: claude + codex, a working board -> installed, doctor OK
  new_user "$base" alice; write_sqlite_config "$base/users/alice/home"; mkdir -p "$base/users/alice/home/.claude"
  # bob: claude, but a swarm job is active -> his run refuses, the others go on
  new_user "$base" bob; write_sqlite_config "$base/users/bob/home"; mkdir -p "$base/users/bob/home/.claude"
  rm -f "$base/users/bob/bin/codex"
  mkdir -p "$base/users/bob/home/.local/state/swarm/active"
  printf '{"job": "bob-job-2026-09"}' > "$base/users/bob/home/.local/state/swarm/active/j.json"
  # carol: codex only, no board config -> stops at "fill in", no credentials invented
  new_user "$base" carol; mkdir -p "$base/users/carol/home/.codex"
  rm -f "$base/users/carol/bin/claude"
  # dave: a human user with neither claude nor codex -> not a candidate, never run
  mkdir -p "$base/users/dave/home"
  # alice has a runtime dir (a lingering user manager); bob has none
  alice_uid="$(awk '$1 == "alice" {print $2}' "$base/uids")"
  mkdir -p "$base/run/user/$alice_uid"
  snapshot_before "$base" alice bob carol dave
  users="alice:$base/users/alice/home;bob:$base/users/bob/home;carol:$base/users/carol/home;dave:$base/users/dave/home"
  set +e
  out="$(run_as_fake_root "$base" "$users" --yes 2>&1)"
  rc=$?
  set -e
  [ "$rc" -ne 0 ] || fail "root run exited 0 although bob's run refused: $out"
  echo "$out" | grep -q "running as root" || fail "root run didn't switch to all-users mode: $out"

  grep -q "^alice " "$base/sudo.log" || fail "alice got no run of her own"
  grep -q "^bob " "$base/sudo.log" || fail "bob got no run of his own"
  grep -q "^carol " "$base/sudo.log" || fail "carol got no run of her own"
  grep -q "^dave " "$base/sudo.log" && fail "dave (no claude/codex) was run anyway"
  grep "^alice " "$base/sudo.log" | grep -q "XDG_RUNTIME_DIR=$base/run/user/$alice_uid bash" \
    || fail "alice's run didn't get her XDG_RUNTIME_DIR: $(cat "$base/sudo.log")"
  grep "^bob " "$base/sudo.log" | grep -q "XDG_RUNTIME_DIR" \
    && fail "bob's run got an XDG_RUNTIME_DIR although he has no runtime dir: $(cat "$base/sudo.log")"
  grep -q -- "--current-user --yes --marketplace $ROOT" "$base/sudo.log" \
    || fail "per-user runs didn't get --current-user --yes and the host flags: $(cat "$base/sudo.log")"

  # each user's own install, in their own home, through their own CLIs
  grep -q "^plugin install swarm@swarm" "$base/users/alice/log/claude.args" || fail "alice: claude plugin not installed"
  grep -q "^plugin add swarm@swarm" "$base/users/alice/log/codex.args" || fail "alice: codex plugin not added"
  [ -f "$base/users/alice/home/.claude/plugins/installed_plugins.json" ] || fail "alice: no installed_plugins.json in her home"
  [ -f "$base/users/alice/home/.local/share/swarm-board/board.sqlite3" ] || fail "alice: no board created in her home"
  grep -q "^plugin install swarm@swarm" "$base/users/bob/log/claude.args" || fail "bob: claude plugin not installed before the refusal"
  grep -q "^plugin add swarm@swarm" "$base/users/carol/log/codex.args" || fail "carol: codex plugin not added"
  [ -f "$base/users/carol/home/.config/swarm/config.toml" ] || fail "carol: bootstrap didn't create her config from the example"
  [ -f "$base/roothome/.config/swarm/config.toml" ] && fail "root got a swarm config of its own"

  # refusals are per user, with their own messages
  echo "$out" | grep -q "bob-job-2026-09" || fail "bob's refusal didn't name his active job: $out"
  echo "$out" | grep -qE "PGPASSWORD=[^.]" && fail "must never print a credential value"

  # USER x HOST summary
  echo "$out" | grep -Eq '^USER +HOST +STATUS$' || fail "no USER x HOST summary header: $out"
  echo "$out" | grep -Eq '^alice +claude +ok$' || fail "summary: alice/claude not ok: $out"
  echo "$out" | grep -Eq '^alice +codex +ok\(/hooks-pending\)$' || fail "summary: alice/codex not ok(/hooks-pending): $out"
  echo "$out" | grep -Eq '^bob +claude +refused\(active-job\)$' || fail "summary: bob/claude not refused(active-job): $out"
  echo "$out" | grep -Eq '^carol +codex +ok\(/hooks-pending\)$' || fail "summary: carol/codex not ok(/hooks-pending): $out"
  echo "$out" | grep -Eq '^dave ' && fail "summary lists dave, who has neither claude nor codex"

  # Codex /hooks step printed per codex user
  echo "$out" | grep -q "sudo -iu alice" || fail "no /hooks step for alice: $out"
  echo "$out" | grep -q "sudo -iu carol" || fail "no /hooks step for carol: $out"
  echo "$out" | grep -q "sudo -iu bob" && fail "a /hooks step for bob, who has no codex"

  grep -q "rebuilt by a root run" "$base/copy.head" && fail "run from a file, the per-user copy should be a plain copy of it"
  assert_root_wrote_nothing "$base" alice bob carol
  [ "$(find "$base/users/dave/home" | sort)" = "$(cat "$base/snap/dave.before")" ] || fail "dave's home was touched"
  echo "root-all-users: ok"
}

# --------------------------------------------------------------------------- test: curl | sudo bash (piped as root)

t_root_piped() {
  base="$(setup_root_env rootpiped)"
  new_user "$base" erin; write_sqlite_config "$base/users/erin/home"; mkdir -p "$base/users/erin/home/.claude"
  rm -f "$base/users/erin/bin/codex"
  snapshot_before "$base" erin
  # sudo kept the invoking user's HOME (macOS's default): root must not create ~/.cache or
  # anything else in it (its scratch dir goes under /tmp).
  invoker="$base/invoker_home"; mkdir -p "$invoker"
  inv_before="$(find "$invoker" | sort)"
  # Under `curl | bash`, BASH_SOURCE[0] is "main": a ./main in the cwd, even one that looks
  # like the installer, must not be taken for it.
  mkdir -p "$base/cwd"
  printf '#!/bin/sh\n# swarm one-shot installer DECOY: not the real one\necho DECOY RAN\n' > "$base/cwd/main"
  out="$(cd "$base/cwd" && env -i HOME="$invoker" PATH="$base/rootbin:$base/idbin:$PYBIN:/usr/bin:/bin:/usr/local/bin" \
      SWARM_VENV="$E2E_VENV" SWARM_NO_MIGRATE=1 SWARM_NO_SYSTEMD=1 SWARM_AUTO_INIT=1 \
      TERM="${TERM:-dumb}" SWARM_INSTALL_TEST_USERS="erin:$base/users/erin/home" \
      SWARM_INSTALL_TEST_RUN_USER_DIR="$base/run/user" \
      bash -s -- --marketplace "$ROOT" --host claude --yes < "$INSTALL_SH" 2>&1)" \
    || fail "piped root run failed: $out"
  grep -q "rebuilt by a root run from a piped install.sh" "$base/copy.head" \
    || fail "piped root run didn't rebuild the installer copy from its own functions (took ./main?): $(cat "$base/copy.head")"
  echo "$out" | grep -q "DECOY RAN" && fail "the ./main decoy was run"
  grep -q -- "--current-user --yes --host claude --marketplace $ROOT" "$base/sudo.log" \
    || fail "piped per-user run didn't get the host flags: $(cat "$base/sudo.log")"
  echo "$out" | grep -Eq '^erin +claude +ok$' || fail "piped: erin/claude not ok: $out"
  echo "$out" | grep -q "^==> done\.$" || fail "piped root run didn't finish: $out"
  [ "$inv_before" = "$(find "$invoker" | sort)" ] \
    || fail "root wrote into the kept HOME $invoker: $(find "$invoker")"
  assert_root_wrote_nothing "$base" erin
  echo "root-piped: ok"
}

# --------------------------------------------------------------------------- test: the installed plugin runs, not the old launcher

t_uses_installed_plugin_not_launcher() {
  ie="$(new_env launcher)"; home="${ie%%:*}"; rest="${ie#*:}"; bindir="${rest%%:*}"; logdir="${rest##*:}"
  write_sqlite_config "$home"
  ver="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "$ROOT/.claude-plugin/plugin.json")"
  # An old frozen tree that also calls itself $ver (an old install under ~/src/swarm-release), with
  # the existing ~/.local/bin/swarm launcher pointing at it...
  frozen="$SCRATCH_ROOT/launcher/swarm-release"
  mkdir -p "$frozen/.claude-plugin" "$frozen/bin"
  printf '{"name": "swarm", "version": "%s"}' "$ver" > "$frozen/.claude-plugin/plugin.json"
  printf '#!/bin/sh\necho "$@" >> "%s/frozen.ran"\nexit 0\n' "$logdir" > "$frozen/bin/swarm"
  chmod +x "$frozen/bin/swarm"
  mkdir -p "$home/.local/bin"
  printf '#!/bin/sh\n# swarm launcher, written by `swarm bootstrap`: runs the installed swarm plugin.\nexec "%s/bin/swarm" "$@"\n' "$frozen" > "$home/.local/bin/swarm"
  chmod +x "$home/.local/bin/swarm"
  # ...and an older version left in Claude's plugin cache: the highest version must win.
  old="$home/.claude/plugins/cache/swarm/swarm/0.9.0"
  mkdir -p "$old/.claude-plugin" "$old/bin"
  printf '{"name": "swarm", "version": "0.9.0"}' > "$old/.claude-plugin/plugin.json"
  printf '#!/bin/sh\necho "$@" >> "%s/oldcache.ran"\nexit 0\n' "$logdir" > "$old/bin/swarm"
  chmod +x "$old/bin/swarm"

  out="$(run_install "$home" "$bindir" --host both --yes 2>&1)" || fail "launcher run failed: $out"
  [ -f "$logdir/frozen.ran" ] && fail "bootstrap/migrate/doctor ran through the old launcher's frozen tree: $(cat "$logdir/frozen.ran")"
  [ -f "$logdir/oldcache.ran" ] && fail "an older cached plugin version ran instead of the newest"
  echo "$out" | grep -q "^==> \[claude\] using $ROOT/bin/swarm$" || fail "claude didn't use the installed plugin's bin/swarm: $out"
  echo "$out" | grep -q "^==> \[codex\] using $home/.codex/plugins/cache/swarm/swarm/local/bin/swarm$" \
    || fail "codex didn't use its plugin cache's bin/swarm: $out"
  target="$(sed -n 's/^exec "\(.*\)\/bin\/swarm" "\$@"$/\1/p' "$home/.local/bin/swarm")"
  [ -n "$target" ] || fail "launcher unreadable after install: $(cat "$home/.local/bin/swarm")"
  [ "$(cd "$target" && pwd -P)" = "$(cd "$ROOT" && pwd -P)" ] \
    || fail "launcher still points at $target, not the installed plugin $ROOT"
  echo "$out" | grep -q "^==> done\.$" || fail "launcher run didn't finish: $out"
  echo "uses-installed-plugin-not-launcher: ok"
}

# --------------------------------------------------------------------------- run everything

t_help
t_no_tty
t_detection
t_idempotent
t_refuses_active_job
t_force_overrides_active_job
t_no_config_file_board
t_non_root_notes_other_users
t_all_users_needs_root
t_root_all_users
t_root_piped
t_uses_installed_plugin_not_launcher

echo "E2E OK: install"
