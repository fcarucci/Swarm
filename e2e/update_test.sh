#!/bin/sh
# End to end tests for `swarm update`, entirely inside scratch HOMEs with stub claude/codex CLIs
# (record their args, never real plugin managers). bin/swarm here (this checkout) plays the role
# of the *currently running* (old) swarm; the checkout itself also plays the role of the *newly
# installed* plugin the stubs "install" -- update.py must re-locate and delegate to it for
# bootstrap/migrate/doctor, which this suite checks by looking for their step output. Old plugin
# roots are small fake directories with just enough (.claude-plugin/plugin.json or
# .codex-plugin/plugin.json, hooks/codex-hooks.json, a dummy executable bin/swarm) to be found and
# version-compared, never actually run. SWARM_VENV points at the checkout's already-built venv so
# bin/swarm needs no pip install (offline, fast). Never touches the real HOME, ~/.claude, ~/.codex,
# or the production swarm skill/board: every HOME below is a fresh mktemp directory.
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
# The venv bin/swarm runs from (the wrapper tests pass a throwaway one; CI has no checkout .venv).
E2E_VENV="${E2E_VENV:-$ROOT/.venv}"
[ -x "$ROOT/bin/swarm" ] || { echo "E2E FAIL: no $ROOT/bin/swarm" >&2; exit 1; }
REAL_VERSION="$(sed -n 's/.*"version": *"\([^"]*\)".*/\1/p' "$ROOT/.claude-plugin/plugin.json" | head -n1)"
[ -n "$REAL_VERSION" ] || { echo "E2E FAIL: can't read this checkout's plugin version" >&2; exit 1; }

fail() { echo "E2E FAIL: $*" >&2; exit 1; }

SCRATCH_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/swarm-update-test.XXXXXX")"
cleanup() { rm -rf "$SCRATCH_ROOT"; }
trap cleanup EXIT INT TERM

# --------------------------------------------------------------------------- fake old plugin root

# $1 = dir to create; $2 = version; $3 = hooks/codex-hooks.json content. A dummy, never-executed
# bin/swarm (just needs to be executable so newest_installed_plugin_root's candidate filter takes
# it): real trees always have one, and update.py must not assume it is runnable before choosing
# whether to run it (only the freshly installed, highest-version one is ever executed).
make_fake_root() {
  dir="$1"; version="$2"; hooks="$3"
  mkdir -p "$dir/.claude-plugin" "$dir/.codex-plugin" "$dir/hooks" "$dir/bin"
  printf '{"name": "swarm", "version": "%s"}' "$version" > "$dir/.claude-plugin/plugin.json"
  printf '{"name": "swarm", "version": "%s"}' "$version" > "$dir/.codex-plugin/plugin.json"
  printf '%s' "$hooks" > "$dir/hooks/codex-hooks.json"
  printf '#!/bin/sh\nexit 0\n' > "$dir/bin/swarm"
  chmod +x "$dir/bin/swarm"
}

# --------------------------------------------------------------------------- stub claude/codex

# $1 = bindir; $2 = logdir; $3 = home; $4 = the plugin root "plugin update"/"add" should install
# (real: $ROOT, so the delegated bootstrap/migrate/doctor actually run); $5 = 1 to make the
# plugin-manager step itself fail (for the "host command failing" scenario).
write_claude_stub() {
  bindir="$1"; logdir="$2"; home="$3"; install_root="$4"; fail_plugin="${5:-0}"
  cat > "$bindir/claude" <<STUB
#!/bin/sh
echo "\$@" >> "$logdir/claude.args"
case "\$1 \$2 \$3" in
  "plugin marketplace update") exit 0 ;;
esac
case "\$1 \$2" in
  "plugin --help")
    printf 'usage: claude plugin <subcommand>\n  install\n  update\n  list\n'; exit 0 ;;
  "plugin update"|"plugin install")
    [ "$fail_plugin" = "1" ] && { echo "claude: plugin update failed: network error" >&2; exit 1; }
    mkdir -p "$home/.claude/plugins"
    printf '{"plugins": {"swarm@swarm": [{"installPath": "%s"}]}}' "$install_root" > "$home/.claude/plugins/installed_plugins.json"
    exit 0 ;;
esac
exit 0
STUB
  chmod +x "$bindir/claude"
}

# $6 = old version string reported by "plugin list --json" before "plugin add" runs; $7 = new
# version string reported after.
write_codex_stub() {
  bindir="$1"; logdir="$2"; home="$3"; new_root="$4"; fail_plugin="${5:-0}"
  old_v="${6:-0.0.9}"; new_v="${7:-$REAL_VERSION}"
  cat > "$bindir/codex" <<STUB
#!/bin/sh
echo "\$@" >> "$logdir/codex.args"
case "\$1 \$2 \$3" in
  "plugin marketplace upgrade")
    [ -f "$logdir/codex.mp_fail" ] && exit 1
    exit 0 ;;
  "plugin marketplace remove") exit 0 ;;
  "plugin marketplace add") exit 0 ;;
esac
case "\$1 \$2" in
  "plugin add")
    [ "$fail_plugin" = "1" ] && { echo "codex: plugin add failed: network error" >&2; exit 1; }
    mkdir -p "$home/.codex/plugins/cache/swarm/swarm"
    ln -sfn "$new_root" "$home/.codex/plugins/cache/swarm/swarm/installed"
    touch "$logdir/codex.installed"
    exit 0 ;;
  "plugin list")
    if [ "\$3" = "--json" ]; then
      if [ -f "$logdir/codex.installed" ]; then v="$new_v"; else v="$old_v"; fi
      printf '{"installed": [{"pluginId": "swarm@swarm", "name": "swarm", "installed": true, "enabled": true, "version": "%s"}], "available": []}' "\$v"
    fi
    exit 0 ;;
esac
exit 0
STUB
  chmod +x "$bindir/codex"
}

# $1 = scratch home dir: a sqlite board config so bootstrap/migrate/doctor have something real
# and offline to work against.
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

new_env() {   # $1 = scenario name -> "home:bindir:logdir"
  n="$1"
  home="$SCRATCH_ROOT/$n/home"
  bindir="$SCRATCH_ROOT/$n/bin"
  logdir="$SCRATCH_ROOT/$n/log"
  mkdir -p "$home" "$bindir" "$logdir"
  echo "$home:$bindir:$logdir"
}

run_update() {   # $1 = home, $2 = bindir, then swarm update args
  home="$1"; bindir="$2"; shift 2
  env -i HOME="$home" PATH="$bindir:/usr/bin:/bin:/usr/local/bin" \
      SWARM_VENV="$E2E_VENV" SWARM_NO_SYSTEMD=1 SWARM_AUTO_INIT=1 \
      CLAUDE_CONFIG_DIR="$home/.claude" CODEX_HOME="$home/.codex" \
      TERM="${TERM:-dumb}" \
      "$ROOT/bin/swarm" update "$@"
}

# --------------------------------------------------------------------------- test: old -> new (claude)

t_update_old_to_new() {
  ie="$(new_env old2new)"; home="${ie%%:*}"; rest="${ie#*:}"; bindir="${rest%%:*}"; logdir="${rest##*:}"
  write_sqlite_config "$home"
  old_root="$SCRATCH_ROOT/old2new/old-root"
  make_fake_root "$old_root" "0.0.9" "OLDHOOKS"
  write_claude_stub "$bindir" "$logdir" "$home" "$ROOT"
  mkdir -p "$home/.claude/plugins"
  printf '{"plugins": {"swarm@swarm": [{"installPath": "%s"}]}}' "$old_root" > "$home/.claude/plugins/installed_plugins.json"

  out="$(run_update "$home" "$bindir" --host claude --no-color)" || fail "old->new update failed: $out"
  echo "$out" | grep -q "0.0.9 -> $REAL_VERSION" || fail "didn't report old->new version: $out"
  echo "$out" | grep -qw "changed" || fail "didn't mark the plugin as changed: $out"
  # bootstrap/doctor from the NEW plugin (this checkout) really ran (its first step is "venv")
  echo "$out" | grep -Eq '^venv ' || fail "bootstrap didn't run from the newly installed plugin: $out"
  echo "$out" | grep -q "Restart your Claude sessions" || fail "didn't print the restart reminder: $out"
  echo "update-old-to-new: ok"
}

# --------------------------------------------------------------------------- test: already up to date

t_already_up_to_date() {
  ie="$(new_env uptodate)"; home="${ie%%:*}"; rest="${ie#*:}"; bindir="${rest%%:*}"; logdir="${rest##*:}"
  write_sqlite_config "$home"
  write_claude_stub "$bindir" "$logdir" "$home" "$ROOT"
  mkdir -p "$home/.claude/plugins"
  printf '{"plugins": {"swarm@swarm": [{"installPath": "%s"}]}}' "$ROOT" > "$home/.claude/plugins/installed_plugins.json"

  out="$(run_update "$home" "$bindir" --host claude --no-color)" || fail "up-to-date run failed: $out"
  echo "$out" | grep -q "swarm is up to date ($REAL_VERSION)" || fail "didn't say up to date: $out"
  echo "$out" | grep -Eq '^venv ' && fail "bootstrap ran despite no version change: $out"
  echo "already-up-to-date: ok"
}

# --------------------------------------------------------------------------- test: --force re-runs anyway

t_force() {
  ie="$(new_env forced)"; home="${ie%%:*}"; rest="${ie#*:}"; bindir="${rest%%:*}"; logdir="${rest##*:}"
  write_sqlite_config "$home"
  write_claude_stub "$bindir" "$logdir" "$home" "$ROOT"
  mkdir -p "$home/.claude/plugins"
  printf '{"plugins": {"swarm@swarm": [{"installPath": "%s"}]}}' "$ROOT" > "$home/.claude/plugins/installed_plugins.json"

  out="$(run_update "$home" "$bindir" --host claude --no-color --force)" || fail "--force run failed: $out"
  echo "$out" | grep -q "swarm is up to date" && fail "--force still short-circuited as up to date: $out"
  echo "$out" | grep -Eq '^venv ' || fail "--force didn't run bootstrap despite no version change: $out"
  echo "force: ok"
}

# --------------------------------------------------------------------------- test: --host codex

t_host_codex() {
  ie="$(new_env codex)"; home="${ie%%:*}"; rest="${ie#*:}"; bindir="${rest%%:*}"; logdir="${rest##*:}"
  write_sqlite_config "$home"
  old_root="$SCRATCH_ROOT/codex/old-root"
  make_fake_root "$old_root" "0.0.9" "OLDHOOKS"
  mkdir -p "$home/.codex/plugins/cache/swarm/swarm"
  ln -sfn "$old_root" "$home/.codex/plugins/cache/swarm/swarm/old"
  write_codex_stub "$bindir" "$logdir" "$home" "$ROOT" 0 "0.0.9" "$REAL_VERSION"

  out="$(run_update "$home" "$bindir" --host codex --no-color)" || fail "codex update failed: $out"
  echo "$out" | grep -q "0.0.9 -> $REAL_VERSION" || fail "codex old->new version not reported: $out"
  echo "$out" | grep -Eq '^venv ' || fail "bootstrap didn't run for codex: $out"
  [ -f "$logdir/claude.args" ] && fail "claude stub was called despite --host codex"
  echo "host-codex: ok"
}

# --------------------------------------------------------------------------- test: a host command fails

t_host_command_fails() {
  ie="$(new_env failing)"; home="${ie%%:*}"; rest="${ie#*:}"; bindir="${rest%%:*}"; logdir="${rest##*:}"
  write_sqlite_config "$home"
  write_claude_stub "$bindir" "$logdir" "$home" "$ROOT" 1   # fail_plugin=1
  mkdir -p "$home/.claude/plugins"
  printf '{"plugins": {"swarm@swarm": [{"installPath": "%s"}]}}' "$ROOT" > "$home/.claude/plugins/installed_plugins.json"

  set +e
  out="$(run_update "$home" "$bindir" --host claude --no-color 2>&1)"
  rc=$?
  set -e
  [ "$rc" -ne 0 ] || fail "run with a failing host command exited 0, expected non-zero"
  echo "$out" | grep -qi "plugin update" || fail "failure didn't name the failing command: $out"
  echo "$out" | grep -qi "network error" || fail "failure didn't carry the CLI's own error text: $out"
  echo "host-command-fails: ok"
}

# --------------------------------------------------------------------------- test: hooks-changed detection

t_hooks_changed_detection() {
  ie="$(new_env hookschanged)"; home="${ie%%:*}"; rest="${ie#*:}"; bindir="${rest%%:*}"; logdir="${rest##*:}"
  write_sqlite_config "$home"
  old_root="$SCRATCH_ROOT/hookschanged/old-root"
  make_fake_root "$old_root" "0.0.9" "OLDHOOKS-DIFFERENT-CONTENT"
  mkdir -p "$home/.codex/plugins/cache/swarm/swarm"
  ln -sfn "$old_root" "$home/.codex/plugins/cache/swarm/swarm/old"
  write_codex_stub "$bindir" "$logdir" "$home" "$ROOT" 0 "0.0.9" "$REAL_VERSION"

  out="$(run_update "$home" "$bindir" --host codex --no-color)" || fail "hooks-changed run failed: $out"
  echo "$out" | grep -q "hooks changed -- start a new Codex session" || fail "hooks changed but the /hooks re-trust reminder wasn't printed: $out"
  echo "hooks-changed-detection: ok"
}

t_hooks_unchanged_no_reminder() {
  ie="$(new_env hooksunchanged)"; home="${ie%%:*}"; rest="${ie#*:}"; bindir="${rest%%:*}"; logdir="${rest##*:}"
  write_sqlite_config "$home"
  old_root="$SCRATCH_ROOT/hooksunchanged/old-root"
  make_fake_root "$old_root" "0.0.9" "placeholder"
  cp "$ROOT/hooks/codex-hooks.json" "$old_root/hooks/codex-hooks.json"   # byte-identical to $ROOT's
  mkdir -p "$home/.codex/plugins/cache/swarm/swarm"
  ln -sfn "$old_root" "$home/.codex/plugins/cache/swarm/swarm/old"
  write_codex_stub "$bindir" "$logdir" "$home" "$ROOT" 0 "0.0.9" "$REAL_VERSION"

  out="$(run_update "$home" "$bindir" --host codex --no-color)" || fail "hooks-unchanged run failed: $out"
  echo "$out" | grep -q "hooks changed -- start a new Codex session" && fail "hooks were unchanged but the /hooks re-trust reminder was printed anyway: $out"
  echo "hooks-unchanged-no-reminder: ok"
}

# --------------------------------------------------------------------------- run everything

t_update_old_to_new
t_already_up_to_date
t_force
t_host_codex
t_host_command_fails
t_hooks_changed_detection
t_hooks_unchanged_no_reminder

echo "E2E OK: update"
