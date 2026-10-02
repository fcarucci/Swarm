"""swarm.update: host detection, version reading, plugin-root discovery, hooks-changed detection,
and the per-host update/failure paths, isolated from real claude/codex CLIs (stub scripts under a
temp $HOME/bin) and from the real HOME. The full orchestration (marketplace + plugin update, then
delegated bootstrap/migrate/doctor from the newly installed plugin) is covered end to end by
e2e/update_test.sh (test_update_sh.py); this file is the fast, unit-level complement.
"""
from __future__ import annotations

import json
import os
import stat
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import ROOT, home_env, posix_only  # noqa: F401

from swarm import update  # noqa: E402


def _write_stub(path: Path, body: str) -> None:
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


class DetectHostsTests(unittest.TestCase):
    def test_host_arg_selects_one(self):
        which = {"claude": "/bin/claude", "codex": "/bin/codex"}.get
        self.assertEqual(update.detect_hosts("claude", which=which), ["claude"])

    def test_host_arg_missing_binary_raises(self):
        which = lambda n: None
        with self.assertRaises(update.UpdateError):
            update.detect_hosts("claude", which=which)

    def test_no_host_arg_finds_every_installed(self):
        which = {"claude": "/bin/claude"}.get
        self.assertEqual(update.detect_hosts(None, which=which), ["claude"])
        which2 = {"claude": "/bin/claude", "codex": "/bin/codex"}.get
        self.assertEqual(update.detect_hosts(None, which=which2), ["claude", "codex"])

    def test_no_host_arg_none_installed_raises(self):
        with self.assertRaises(update.UpdateError):
            update.detect_hosts(None, which=lambda n: None)

    def test_host_both_finds_every_installed(self):
        which = {"claude": "/bin/claude", "codex": "/bin/codex"}.get
        self.assertEqual(update.detect_hosts("both", which=which), ["claude", "codex"])


class VersionAndRootTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="swarm-update-"))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        p = mock.patch.dict(os.environ, {**home_env(self.home)})
        p.start(); self.addCleanup(p.stop)

    def _plugin_tree(self, name: str, version: str) -> Path:
        t = self.home / name
        (t / ".claude-plugin").mkdir(parents=True)
        (t / ".claude-plugin/plugin.json").write_text(json.dumps({"name": "swarm", "version": version}))
        (t / "bin").mkdir()
        _write_stub(t / "bin" / "swarm", "exit 0")
        return t

    def test_codex_plugin_version_none_on_unexpected_json_shapes(self):
        # A malformed or unexpected `codex plugin list --json` answer (not a dict, or "installed"
        # not a list, or a list entry that isn't a dict) must return None, never raise
        # AttributeError/TypeError.
        codex_bin = self.home / "bin" / "codex"
        codex_bin.parent.mkdir(parents=True)
        for payload in ('["not", "a", "dict"]', '"just a string"', '42',
                        '{"installed": "not a list"}', '{"installed": ["not a dict"]}',
                        '{"installed": [{"pluginId": "swarm@swarm"}]}'):  # dict but no version
            with self.subTest(payload=payload):
                _write_stub(codex_bin, f'''
case "$1 $2" in
  "plugin list") [ "$3" = "--json" ] && printf '%s' '{payload}'; exit 0 ;;
esac
exit 0
''')
                self.assertIsNone(update._codex_plugin_version(str(codex_bin)))

    def test_claude_plugin_version_reads_installed_plugins_json(self):
        tree = self._plugin_tree("swarm-1", "2.3.4")
        plugins_dir = self.home / ".claude" / "plugins"
        plugins_dir.mkdir(parents=True)
        (plugins_dir / "installed_plugins.json").write_text(
            json.dumps({"plugins": {"swarm@swarm": [{"installPath": str(tree)}]}}))
        self.assertEqual(update._claude_plugin_version(plugins_dir), "2.3.4")
        self.assertEqual(update._claude_plugin_root(plugins_dir), tree)

    def test_claude_plugin_version_none_when_not_installed(self):
        plugins_dir = self.home / ".claude" / "plugins"
        plugins_dir.mkdir(parents=True)
        (plugins_dir / "installed_plugins.json").write_text(json.dumps({"plugins": {}}))
        self.assertIsNone(update._claude_plugin_version(plugins_dir))

    def test_claude_plugin_version_none_when_file_missing(self):
        plugins_dir = self.home / ".claude" / "plugins"
        self.assertIsNone(update._claude_plugin_version(plugins_dir))

    def test_newest_installed_plugin_root_picks_highest_version(self):
        old = self._plugin_tree("old", "0.1.0")
        new = self._plugin_tree("new", "0.2.0")
        cache = self.home / ".codex" / "plugins" / "cache" / "swarm" / "swarm"
        cache.mkdir(parents=True)
        (cache / "a").symlink_to(old)
        (cache / "b").symlink_to(new)
        picked = update.newest_installed_plugin_root("codex", codex_home=self.home / ".codex")
        self.assertEqual(picked.resolve(), new.resolve())

    def test_newest_installed_plugin_root_skips_non_executable_bin(self):
        tree = self.home / "broken"
        (tree / ".codex-plugin").mkdir(parents=True)
        (tree / ".codex-plugin/plugin.json").write_text(json.dumps({"name": "swarm", "version": "9.9.9"}))
        # no bin/swarm at all: must be skipped even though its version is the highest
        cache = self.home / ".codex" / "plugins" / "cache" / "swarm" / "swarm"
        cache.mkdir(parents=True)
        (cache / "a").symlink_to(tree)
        good = self._plugin_tree("good", "0.1.0")
        (cache / "b").symlink_to(good)
        picked = update.newest_installed_plugin_root("codex", codex_home=self.home / ".codex")
        self.assertEqual(picked.resolve(), good.resolve())

    def test_newest_installed_plugin_root_none_when_nothing_found(self):
        self.assertIsNone(update.newest_installed_plugin_root("codex", codex_home=self.home / ".codex"))

    def test_newest_installed_plugin_root_codex_prefers_reported_version(self):
        # The bug this guards against: a stale higher-numbered cache directory (e.g. an old
        # 1.0.0 left behind after upgrading to 0.1.0) must not outrank the version `codex plugin
        # list --json` actually reports as installed.
        stale = self._plugin_tree("stale", "1.0.0")
        reported = self._plugin_tree("reported", "0.1.0")
        cache = self.home / ".codex" / "plugins" / "cache" / "swarm" / "swarm"
        cache.mkdir(parents=True)
        (cache / "a").symlink_to(stale)
        (cache / "b").symlink_to(reported)
        codex_bin = self.home / "bin" / "codex"
        codex_bin.parent.mkdir(parents=True)
        _write_stub(codex_bin, '''
case "$1 $2" in
  "plugin list") [ "$3" = "--json" ] && printf '{"installed": [{"pluginId": "swarm@swarm", "name": "swarm", "installed": true, "version": "0.1.0"}]}'; exit 0 ;;
esac
exit 0
''')
        picked = update.newest_installed_plugin_root("codex", codex_home=self.home / ".codex",
                                                      codex_bin=str(codex_bin))
        self.assertEqual(picked.resolve(), reported.resolve())

    def test_newest_installed_plugin_root_codex_falls_back_when_nothing_reported(self):
        # "codex plugin list --json" gives no usable version (unsupported CLI, or the plugin
        # isn't reported installed at all): falls back to the cache glob's highest version, same
        # as before this fix.
        old = self._plugin_tree("old", "0.1.0")
        new = self._plugin_tree("new", "0.2.0")
        cache = self.home / ".codex" / "plugins" / "cache" / "swarm" / "swarm"
        cache.mkdir(parents=True)
        (cache / "a").symlink_to(old)
        (cache / "b").symlink_to(new)
        codex_bin = self.home / "bin" / "codex"
        codex_bin.parent.mkdir(parents=True)
        _write_stub(codex_bin, 'exit 1\n')
        picked = update.newest_installed_plugin_root("codex", codex_home=self.home / ".codex",
                                                      codex_bin=str(codex_bin))
        self.assertEqual(picked.resolve(), new.resolve())


class HooksChangedTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="swarm-update-hooks-"))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)

    def _root(self, name: str, hooks_content: str | None) -> Path:
        t = self.home / name
        (t / "hooks").mkdir(parents=True)
        if hooks_content is not None:
            (t / "hooks" / "codex-hooks.json").write_text(hooks_content)
        return t

    def test_changed_when_content_differs(self):
        old = self._root("old", '{"a": 1}')
        new = self._root("new", '{"a": 2}')
        self.assertTrue(update.codex_hooks_changed(old, new))

    def test_unchanged_when_content_identical(self):
        old = self._root("old", '{"a": 1}')
        new = self._root("new", '{"a": 1}')
        self.assertFalse(update.codex_hooks_changed(old, new))

    def test_none_root_never_changed(self):
        new = self._root("new", '{"a": 1}')
        self.assertFalse(update.codex_hooks_changed(None, new))
        self.assertFalse(update.codex_hooks_changed(new, None))

    def test_missing_file_is_not_a_crash(self):
        old = self._root("old", None)   # no hooks/codex-hooks.json at all
        new = self._root("new", '{"a": 1}')
        self.assertTrue(update.codex_hooks_changed(old, new))


class PerHostUpdateFailureTests(unittest.TestCase):
    """update_claude/update_codex against stub CLIs: a failing host command must raise
    UpdateError carrying the CLI's own stderr, never silently succeed or swallow it."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="swarm-update-fail-"))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        p = mock.patch.dict(os.environ, {**home_env(self.home)})
        p.start(); self.addCleanup(p.stop)
        self.bin = self.home / "bin"
        self.bin.mkdir()

    @posix_only("runs a POSIX sh script (the Windows entry points are tested in test_windows_*.py)")
    def test_update_claude_raises_on_marketplace_failure(self):
        stub = self.bin / "claude"
        _write_stub(stub, 'echo "boom: no route to host" >&2; exit 1')
        with self.assertRaises(update.UpdateError) as ctx:
            update.update_claude(str(stub))
        self.assertIn("no route to host", str(ctx.exception))

    @posix_only("runs a POSIX sh script (the Windows entry points are tested in test_windows_*.py)")
    def test_update_claude_raises_on_plugin_failure_with_cli_text(self):
        stub = self.bin / "claude"
        _write_stub(stub, '''
case "$1 $2 $3" in
  "plugin marketplace update") exit 0 ;;
esac
case "$1 $2" in
  "plugin --help") echo "  update"; exit 0 ;;
  "plugin update") echo "network unreachable" >&2; exit 1 ;;
esac
exit 0
''')
        with self.assertRaises(update.UpdateError) as ctx:
            update.update_claude(str(stub))
        self.assertIn("network unreachable", str(ctx.exception))

    def test_update_claude_falls_back_to_install_without_update_subcommand(self):
        stub = self.bin / "claude"
        installed = self.home / "installed.marker"
        _write_stub(stub, f'''
case "$1 $2 $3" in
  "plugin marketplace update") exit 0 ;;
esac
case "$1 $2" in
  "plugin --help") echo "  install"; echo "  list"; exit 0 ;;
  "plugin install") touch "{installed}"; exit 0 ;;
  "plugin update") echo "no such subcommand" >&2; exit 1 ;;
esac
exit 0
''')
        result = update.update_claude(str(stub))
        self.assertTrue(installed.exists())
        self.assertEqual(result["verb"], "install")

    def test_update_codex_raises_on_plugin_add_failure(self):
        stub = self.bin / "codex"
        _write_stub(stub, '''
case "$1 $2 $3" in
  "plugin marketplace upgrade") exit 0 ;;
esac
case "$1 $2" in
  "plugin list") [ "$3" = "--json" ] && printf '{"installed": []}'; exit 0 ;;
  "plugin add") echo "codex: add failed: disk full" >&2; exit 1 ;;
esac
exit 0
''')
        with self.assertRaises(update.UpdateError) as ctx:
            update.update_codex(str(stub))
        self.assertIn("disk full", str(ctx.exception))

    def test_update_codex_falls_back_to_remove_add_marketplace_configured_source(self):
        # When "marketplace update" fails, the fallback must re-add whatever source the
        # marketplace is *currently* configured with (read via "marketplace list --json"), never
        # a hardcoded default -- otherwise a user who added the marketplace from a local frozen
        # tree or a different remote would be silently switched to another source.
        stub = self.bin / "codex"
        calls = self.home / "calls.log"
        _write_stub(stub, f'''
echo "$@" >> "{calls}"
case "$1 $2 $3" in
  "plugin marketplace upgrade") exit 1 ;;
  "plugin marketplace remove") exit 0 ;;
  "plugin marketplace add") exit 0 ;;
  "plugin marketplace list") [ "$4" = "--json" ] && printf '[{{"name": "swarm", "source": "git@example.com:configured/source.git"}}]'; exit 0 ;;
esac
case "$1 $2" in
  "plugin list") [ "$3" = "--json" ] && printf '{{"installed": []}}'; exit 0 ;;
  "plugin add") exit 0 ;;
esac
exit 0
''')
        update.update_codex(str(stub))
        log = calls.read_text()
        self.assertIn("plugin marketplace remove swarm", log)
        self.assertIn("plugin marketplace add git@example.com:configured/source.git", log)
        self.assertNotIn(update.DEFAULT_MARKETPLACE, log)

    def test_update_codex_dead_local_source_falls_back_to_public_source(self):
        # A configured source that is a local path which no longer exists (a cleaned-up codex staging
        # dir) can never be re-added: use the public source, and don't keep the dead path.
        stub = self.bin / "codex"
        calls = self.home / "calls.log"
        dead = str(self.home / "gone" / "marketplaces" / "swarm")
        _write_stub(stub, f'''
echo "$@" >> "{calls}"
case "$1 $2 $3" in
  "plugin marketplace upgrade") exit 1 ;;
  "plugin marketplace remove") exit 0 ;;
  "plugin marketplace add") exit 0 ;;
  "plugin marketplace list") [ "$4" = "--json" ] && printf '{{"marketplaces": [{{"name": "swarm", "marketplaceSource": {{"sourceType": "local", "source": "{dead}"}}}}]}}'; exit 0 ;;
esac
case "$1 $2" in
  "plugin list") [ "$3" = "--json" ] && printf '{{"installed": []}}'; exit 0 ;;
  "plugin add") exit 0 ;;
esac
exit 0
''')
        update.update_codex(str(stub))
        log = calls.read_text()
        self.assertIn("plugin marketplace remove swarm", log)
        self.assertIn(f"plugin marketplace add {update.DEFAULT_MARKETPLACE}", log)
        self.assertNotIn(dead, log.split("plugin marketplace add", 1)[1])

    def test_update_codex_unregistered_marketplace_is_added_from_the_public_source(self):
        # An earlier failed remove+add left no swarm marketplace: nothing to remove, add the public one.
        stub = self.bin / "codex"
        calls = self.home / "calls.log"
        _write_stub(stub, f'''
echo "$@" >> "{calls}"
case "$1 $2 $3" in
  "plugin marketplace upgrade") echo "error: marketplace swarm is not installed" >&2; exit 1 ;;
  "plugin marketplace add") exit 0 ;;
  "plugin marketplace list") [ "$4" = "--json" ] && printf '{{"marketplaces": []}}'; exit 0 ;;
esac
case "$1 $2" in
  "plugin list") [ "$3" = "--json" ] && printf '{{"installed": []}}'; exit 0 ;;
  "plugin add") exit 0 ;;
esac
exit 0
''')
        update.update_codex(str(stub))
        log = calls.read_text()
        self.assertNotIn("plugin marketplace remove", log)
        self.assertIn(f"plugin marketplace add {update.DEFAULT_MARKETPLACE}", log)

    def test_update_codex_marketplace_failure_stops_when_source_unknown(self):
        # "marketplace list" gives no usable source (unsupported/empty): update_codex must stop
        # with the original error, never guess a source to remove+add.
        stub = self.bin / "codex"
        calls = self.home / "calls.log"
        _write_stub(stub, f'''
echo "$@" >> "{calls}"
case "$1 $2 $3" in
  "plugin marketplace upgrade") echo "codex: marketplace update failed: timeout" >&2; exit 1 ;;
  "plugin marketplace list") exit 0 ;;
esac
exit 0
''')
        with self.assertRaises(update.UpdateError) as ctx:
            update.update_codex(str(stub))
        self.assertIn("timeout", str(ctx.exception))
        log = calls.read_text()
        self.assertNotIn("plugin marketplace remove", log)
        self.assertNotIn("plugin marketplace add", log)

    def test_update_codex_prefers_upgrade_subcommand(self):
        # Newer codex renamed `plugin marketplace update` to `upgrade` (same optional
        # marketplace-name argument): use it, and never call the old name.
        stub = self.bin / "codex"
        calls = self.home / "calls.log"
        _write_stub(stub, f'''
echo "$@" >> "{calls}"
case "$1 $2 $3" in
  "plugin marketplace update") echo "error: unrecognized subcommand 'update'" >&2; exit 2 ;;
esac
exit 0
''')
        update.update_codex(str(stub))
        log = calls.read_text()
        self.assertIn("plugin marketplace upgrade swarm", log)
        self.assertNotIn("plugin marketplace update", log)
        self.assertNotIn("plugin marketplace remove", log)

    def test_update_codex_falls_back_to_update_on_older_cli(self):
        # An older CLI has no `upgrade`: clap says "unrecognized subcommand" and we retry `update`.
        stub = self.bin / "codex"
        calls = self.home / "calls.log"
        _write_stub(stub, f'''
echo "$@" >> "{calls}"
case "$1 $2 $3" in
  "plugin marketplace upgrade") echo "error: unrecognized subcommand 'upgrade'" >&2; exit 2 ;;
esac
exit 0
''')
        update.update_codex(str(stub))
        log = calls.read_text()
        self.assertIn("plugin marketplace upgrade swarm", log)
        self.assertIn("plugin marketplace update swarm", log)
        self.assertNotIn("plugin marketplace remove", log)

    def test_update_codex_real_upgrade_failure_does_not_try_update(self):
        # A genuine upgrade failure (network, etc.) is not an old CLI: no `update` retry, and the
        # remove+add fallback runs off the configured source.
        stub = self.bin / "codex"
        calls = self.home / "calls.log"
        _write_stub(stub, f'''
echo "$@" >> "{calls}"
case "$1 $2 $3" in
  "plugin marketplace upgrade") echo "network down" >&2; exit 1 ;;
  "plugin marketplace list") printf '{{"marketplaces":[{{"name":"swarm","root":"/r","marketplaceSource":{{"sourceType":"git","source":"https://example.com/x.git"}}}}]}}'; exit 0 ;;
esac
exit 0
''')
        update.update_codex(str(stub))
        log = calls.read_text()
        self.assertNotIn("plugin marketplace update", log)
        self.assertIn("plugin marketplace add https://example.com/x.git", log)

    def test_update_codex_source_from_config_toml_when_list_lacks_it(self):
        # `marketplace list` fails or omits the source: read [marketplaces.swarm] from config.toml.
        (self.home / ".codex").mkdir()
        (self.home / ".codex" / "config.toml").write_text(
            '[marketplaces.other]\nsource = "https://example.com/other.git"\n\n'
            '[marketplaces.swarm]\nsource_type = "git"\nsource = "https://example.com/x.git"\nref = "stable"\n')
        p = mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home / ".codex")}); p.start(); self.addCleanup(p.stop)
        stub = self.bin / "codex"
        calls = self.home / "calls.log"
        _write_stub(stub, f'''
echo "$@" >> "{calls}"
case "$1 $2 $3" in
  "plugin marketplace upgrade") exit 1 ;;
  "plugin marketplace list") echo "boom" >&2; exit 1 ;;
esac
exit 0
''')
        update.update_codex(str(stub))
        self.assertIn("plugin marketplace add https://example.com/x.git --ref stable", calls.read_text())

    def test_codex_table_root_is_never_taken_as_source(self):
        # The human table is `MARKETPLACE  ROOT` (a local snapshot dir), not a source: re-adding
        # it would silently turn a git marketplace into a local one.
        stub = self.bin / "codex"
        _write_stub(stub, '''
case "$1 $2 $3" in
  "plugin marketplace list") [ "$4" = "--json" ] && exit 1; printf 'MARKETPLACE  ROOT\nswarm        /home/u/.codex/.tmp/marketplaces/swarm\n'; exit 0 ;;
esac
exit 0
''')
        self.assertIsNone(update._codex_marketplace_source(str(stub)))


if __name__ == "__main__":
    unittest.main()


class _BlockModule:
    """A meta_path finder that makes one module unimportable, like a plugin folder `swarm update`
    just replaced under the running process."""

    def __init__(self, name):
        self.name = name

    def find_spec(self, name, path=None, target=None):
        if name == self.name:
            raise ModuleNotFoundError(f"No module named '{name}'", name=name)
        return None


class SourceReplacedMidUpdateTests(unittest.TestCase):
    """`swarm update` runs from the plugin cache it updates: once the host CLI swaps the old version
    folder out, nothing more can be imported from it. Run as `python -m swarm.cli`, the CLI is
    `__main__`, not `swarm.cli`, so a lazy `from swarm.cli import ...` after the swap failed with
    ModuleNotFoundError (seen on Codex 0.1.7 -> 0.1.8)."""

    def test_the_summary_prints_after_the_running_plugin_folder_is_gone(self):
        import io
        import sys
        import swarm
        saved = {k: v for k, v in sys.modules.items() if k == "swarm.cli"}
        sys.modules.pop("swarm.cli", None)          # as under `python -m swarm.cli`
        if hasattr(swarm, "cli"):
            self.addCleanup(setattr, swarm, "cli", swarm.cli)
            del swarm.cli
        blocker = _BlockModule("swarm.cli")
        self.addCleanup(sys.modules.update, saved)
        self.addCleanup(lambda: blocker in sys.meta_path and sys.meta_path.remove(blocker))

        def swapped_out(_bin, *_pin):
            sys.meta_path.insert(0, blocker)       # the old folder is gone from here on
            return {"changed": True, "old_version": "0.1.7", "new_version": "0.1.8",
                    "old_root": None, "new_root": None, "verb": "update"}

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(update, "update_claude", side_effect=swapped_out), \
                mock.patch.object(update.channels, "latest_tag", return_value=None), \
                mock.patch.object(update, "newest_installed_plugin_root", return_value=None), \
                mock.patch("sys.stderr", err):
            rc = update.run_update("claude", False, False, which=lambda h: f"/bin/{h}", out=out)
        self.assertIn("claude plugin changed  0.1.7 -> 0.1.8", out.getvalue())
        self.assertEqual(rc, 1)                     # stops at the missing bin/swarm, not a traceback
        self.assertIn("can't find the installed swarm plugin", err.getvalue())


class UpgradeAlwaysForcesMigrateTests(unittest.TestCase):
    """`swarm upgrade` always runs migrate with --force: an active job only warns, never blocks."""

    def test_migrate_is_forced_without_force_flag(self):
        import io
        seen = {}
        root = Path(tempfile.mkdtemp(prefix="swarm-upg-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        (root / "bin").mkdir()
        _write_stub(root / "bin" / "swarm", "exit 0")
        changed = {"changed": True, "old_version": "0.1.10", "new_version": "0.1.11",
                   "old_root": None, "new_root": root, "verb": "update"}
        def fake_migrate(sw, force, color, config_path):
            seen["force"] = force
            return 0
        with mock.patch.object(update, "update_claude", return_value=changed), \
                mock.patch.object(update.channels, "latest_tag", return_value=None), \
                mock.patch.object(update, "_run_migrate", side_effect=fake_migrate), \
                mock.patch.object(update, "_child", return_value=(0, "", "")):
            update.run_update("claude", False, False, which=lambda h: f"/bin/{h}", out=io.StringIO())
        self.assertIs(seen.get("force"), True)
