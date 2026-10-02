from __future__ import annotations

import os
import stat
import shutil
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from support import ROOT, base_config, home_env, posix_only  # noqa: F401

from swarm import codex_config as cc  # noqa: E402

USER = """# my codex config
model = "gpt-5.5-codex"   # keep this comment
agents.max_depth = 1

[sandbox_workspace_write]
# roots I need
writable_roots = ["/home/u/work"]

[mcp_servers.x]
command = "x"
"""


class CodexConfigEditTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="swarm-cc-"))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        p = mock.patch.dict(os.environ, {**home_env(self.home)}); p.start(); self.addCleanup(p.stop)
        self.path = self.home / ".codex/config.toml"
        self.path.parent.mkdir(parents=True)
        self.cfg = base_config(spool_dir=str(self.home / "spool"))

    def test_edits_only_swarm_keys_and_keeps_the_rest(self):
        self.path.write_text(USER)
        status, detail = cc.apply(self.path, self.cfg)
        self.assertEqual(status, "changed")
        new = self.path.read_text()
        d = tomllib.loads(new)
        self.assertEqual(d["agents"]["max_depth"], 2)
        self.assertNotIn("network_access", d["sandbox_workspace_write"])      # never set in the base table
        roots = d["sandbox_workspace_write"]["writable_roots"]
        self.assertEqual(roots[0], "/home/u/work")
        self.assertNotIn(str(self.home / ".local/state/swarm"), roots)         # never the state dir itself
        self.assertIn(str(self.home / "spool"), roots)
        for keep in ("# my codex config", '# keep this comment', "# roots I need", '[mcp_servers.x]', 'command = "x"'):
            self.assertIn(keep, new)
        self.assertEqual(len(list(self.path.parent.glob("config.toml.pre-swarm-*"))), 1)
        self.assertIn("agents.max_depth = 2", detail)       # the changed keys, with the swarm's values
        self.assertIn("writable_roots: added", detail)
        self.assertEqual(cc.apply(self.path, self.cfg)[0], "ok")   # second run: nothing to do

    def test_missing_file_and_tables_are_created(self):
        status, _ = cc.apply(self.path, self.cfg)
        self.assertEqual(status, "changed")
        d = tomllib.loads(self.path.read_text())
        self.assertEqual(d["agents"]["max_depth"], 2)

    def test_multiline_array_is_left_to_the_user(self):
        self.path.write_text('[sandbox_workspace_write]\nwritable_roots = [\n  "/a",\n]\n')
        before = self.path.read_text()
        status, detail = cc.apply(self.path, self.cfg)
        self.assertEqual(status, "manual")
        self.assertEqual(self.path.read_text(), before)
        self.assertIn("writable_roots", detail)

    def test_inline_table_is_left_to_the_user(self):
        self.path.write_text('sandbox_workspace_write = { network_access = false }\n')
        self.assertEqual(cc.apply(self.path, self.cfg)[0], "manual")

    def test_higher_max_depth_is_kept(self):
        self.path.write_text("[agents]\nmax_depth = 4\n")
        cc.apply(self.path, self.cfg)
        self.assertEqual(tomllib.loads(self.path.read_text())["agents"]["max_depth"], 4)

    @posix_only("needs POSIX file modes (Windows has ACLs)")
    def test_mode_kept_and_detail_shows_no_unrelated_value(self):
        secret = 'api_key = "sk-test-secret-value-0123456789"'
        self.path.write_text(USER + f"\n[mcp_servers.x.env]\n{secret}\n")
        self.path.chmod(0o600)
        status, detail = cc.apply(self.path, self.cfg)
        self.assertEqual(status, "changed")
        self.assertEqual(oct(self.path.stat().st_mode & 0o777), "0o600")
        (backup,) = self.path.parent.glob("config.toml.pre-swarm-*")
        self.assertEqual(oct(backup.stat().st_mode & 0o777), "0o600")
        for leaked in ("sk-test-secret", "gpt-5.5-codex", "/home/u/work", "keep this comment", "mcp_servers"):
            self.assertNotIn(leaked, detail)
        self.assertIn(secret, self.path.read_text())            # untouched in the file itself

    def test_read_only_mode_is_left_and_reported(self):
        self.path.write_text('sandbox_mode = "read-only"\n')
        status, detail = cc.apply(self.path, self.cfg)
        self.assertEqual(status, "manual")
        self.assertIn('sandbox_mode = "workspace-write"', detail)
        self.assertEqual(tomllib.loads(self.path.read_text())["sandbox_mode"], "read-only")

    def test_read_only_via_selected_profile(self):
        d = tomllib.loads('profile = "p"\n[profiles.p]\nsandbox_mode = "read-only"\n')
        self.assertEqual(cc.effective_sandbox_mode(d), "read-only")
        self.assertIsNone(cc.effective_sandbox_mode({}))

    def test_profile_files_that_override_are_reported_by_name_only(self):
        (self.path.parent / "fast.config.toml").write_text('sandbox_mode = "read-only"\ntoken = "sk-secret-in-profile"\n')
        (self.path.parent / "style.config.toml").write_text('model = "x"\n')          # no sandbox keys: not reported
        status, detail = cc.apply(self.path, self.cfg)
        self.assertEqual(status, "manual")
        self.assertIn("fast", detail)
        self.assertNotIn("style", detail)
        self.assertNotIn("sk-secret", detail)
        self.assertIn('sandbox_mode = "read-only"', (self.path.parent / "fast.config.toml").read_text())   # untouched
        self.assertEqual(cc.profile_overrides(self.path.parent, {"profiles": {"old": {"agents": {}}}}), ["fast", "old"])

    def test_relative_dirs_become_absolute_roots(self):
        cfg = base_config(spool_dir="rel/spool")
        cfg["hook"]["marker_dir"] = "./markers"
        # apply() now mkdirs every writable root (see test_apply_creates_the_writable_root_dirs):
        # run from a throwaway cwd so a relative spool_dir/marker_dir can't create real
        # directories next to the checkout.
        real_cwd = os.getcwd()
        cwd_dir = tempfile.mkdtemp(prefix="swarm-cc-cwd-")
        self.addCleanup(shutil.rmtree, cwd_dir, ignore_errors=True)
        os.chdir(cwd_dir)
        self.addCleanup(os.chdir, real_cwd)
        cwd = os.getcwd()
        roots = cc.required(cfg)[("sandbox_workspace_write", "writable_roots")]
        self.assertTrue(all(os.path.isabs(r) for r in roots), roots)
        self.assertIn(os.path.join(cwd, "rel/spool"), roots)
        self.assertIn(os.path.join(cwd, "markers"), roots)
        inside = base_config(spool_dir="~/.local/state/swarm/../swarm/spool")   # normalized, and granted itself
        self.assertEqual(cc.required(inside)[("sandbox_workspace_write", "writable_roots")],
                         [str(self.home / ".local/state/swarm/spool"), str(self.home / ".local/state/swarm/active")])
        cc.apply(self.path, cfg)
        written = tomllib.loads(self.path.read_text())["sandbox_workspace_write"]["writable_roots"]
        self.assertTrue(all(os.path.isabs(r) for r in written))

    @posix_only("needs POSIX file modes (Windows has ACLs)")
    def test_apply_creates_the_writable_root_directories(self):
        """Codex's sandbox bind-mounts each writable root at session start: a root that doesn't
        exist yet makes the mount (and so every command) fail before the swarm ever gets a chance
        to create it lazily on first use (a real bug found in a live Codex run). Every
        created root is private (0700, the spool contract, kept for state/marker roots too)."""
        cfg = base_config(spool_dir=str(self.home / "scratch/spool"))
        cfg["hook"]["marker_dir"] = str(self.home / "scratch/markers")
        for root in cc.required(cfg)[("sandbox_workspace_write", "writable_roots")]:
            self.assertFalse(Path(root).exists(), root)
        cc.apply(self.path, cfg)
        for root in cc.required(cfg)[("sandbox_workspace_write", "writable_roots")]:
            p = Path(root)
            self.assertTrue(p.is_dir(), root)
            self.assertEqual(oct(p.stat().st_mode & 0o777), "0o700", root)

    @posix_only("needs POSIX file modes (Windows has ACLs)")
    def test_apply_tightens_a_loose_existing_root(self):
        cfg = base_config(spool_dir=str(self.home / "scratch/spool"))
        cfg["hook"]["marker_dir"] = str(self.home / "scratch/markers")
        spool = Path(cfg["board"]["spool_dir"])
        spool.mkdir(parents=True, mode=0o755)
        os.chmod(spool, 0o755)   # mkdir's mode is masked by umask; force it loose regardless
        cc.apply(self.path, cfg)
        self.assertEqual(oct(spool.stat().st_mode & 0o777), "0o700")

    @posix_only("needs POSIX file modes (Windows has ACLs)")
    def test_apply_forces_0700_on_creation_despite_a_restrictive_umask(self):
        """mkdir's mode argument is masked by the umask: a restrictive one (e.g. 0o277) could
        leave a brand-new root without owner write if nothing forced it back to exactly 0700."""
        cfg = base_config(spool_dir=str(self.home / "scratch/spool"))
        cfg["hook"]["marker_dir"] = str(self.home / "scratch/markers")
        # pre-create the parents under a normal umask: 0o277 is only interesting for the leaf
        # root itself here -- with it in effect for the parents too, mkdir -p can't even create
        # them (each loses its own owner-write bit, so it can't create the next level down),
        # which is a different, unrelated failure from the one this test is about.
        for root in cc.required(cfg)[("sandbox_workspace_write", "writable_roots")]:
            Path(root).parent.mkdir(parents=True, exist_ok=True)
        old_umask = os.umask(0o277)
        try:
            status, detail = cc.apply(self.path, cfg)
        finally:
            os.umask(old_umask)
        self.assertIn(status, ("changed", "ok"), detail)
        for root in cc.required(cfg)[("sandbox_workspace_write", "writable_roots")]:
            st = Path(root).stat()
            self.assertEqual(oct(stat.S_IMODE(st.st_mode)), "0o700", root)
            self.assertTrue(os.access(root, os.W_OK), root)   # not just the bit: actually writable

    def test_apply_refuses_an_existing_root_the_owner_cant_write(self):
        cfg = base_config(spool_dir=str(self.home / "scratch/spool"))
        cfg["hook"]["marker_dir"] = str(self.home / "scratch/markers")
        spool = Path(cfg["board"]["spool_dir"])
        spool.mkdir(parents=True)
        spool.chmod(0o500)                # owner can read/traverse but not write: unusable as-is
        self.addCleanup(spool.chmod, 0o700)
        status, detail = cc.apply(self.path, cfg)
        self.assertEqual(status, "manual")
        self.assertIn("0o500", detail)
        self.assertEqual(oct(spool.stat().st_mode & 0o777), "0o500")   # left alone: never widened
        self.assertFalse(self.path.exists())                          # config.toml is never written

    def test_apply_refuses_before_writing_when_a_root_is_a_file(self):
        cfg = base_config(spool_dir=str(self.home / "scratch/spool"))
        cfg["hook"]["marker_dir"] = str(self.home / "scratch/markers")
        spool = Path(cfg["board"]["spool_dir"])
        spool.parent.mkdir(parents=True)
        spool.write_text("not a directory")
        status, detail = cc.apply(self.path, cfg)
        self.assertEqual(status, "manual")
        self.assertIn("not a directory", detail)
        self.assertFalse(self.path.exists())   # config.toml is never written when a root is unusable

    def test_apply_refuses_before_writing_when_a_root_cannot_be_created(self):
        cfg = base_config(spool_dir=str(self.home / "scratch/spool"))
        cfg["hook"]["marker_dir"] = str(self.home / "scratch/markers")
        with mock.patch.object(cc.Path, "mkdir", side_effect=OSError("no space left on device")):
            status, detail = cc.apply(self.path, cfg)
        self.assertEqual(status, "manual")
        self.assertIn("no space left", detail)
        self.assertFalse(self.path.exists())

    def test_unsupported_shapes_are_manual_before_any_edit(self):
        for text, reason in (('[sandbox_workspace_write]\nwritable_roots = "/a"\n', "not a list of strings"),
                             ('[sandbox_workspace_write]\nwritable_roots = ["/a", 1]\n', "not a list of strings"),
                             ('sandbox_workspace_write = "x"\n', "not a table"),
                             ('agents = 3\n', "not a table"),
                             ('[agents]\nmax_depth = "2"\n', "not an integer"),
                             ('[agents]\nmax_depth = true\n', "not an integer"),
                             ('[sandbox_workspace_write]\nnetwork_access = "yes"\n', "not true/false")):
            self.path.write_text(text)
            status, detail = cc.apply(self.path, self.cfg)
            self.assertEqual(status, "manual", text)
            self.assertIn(reason, detail)
            self.assertEqual(self.path.read_text(), text)          # nothing edited
            self.assertEqual(list(self.path.parent.glob("config.toml.pre-swarm-*")), [])

    def test_inline_comments_on_edited_lines_are_kept(self):
        self.path.write_text('agents.max_depth = 1  # was one\n\n[sandbox_workspace_write]\n'
                             'network_access = false # "quoted # not a comment"\n'
                             'writable_roots = ["/a#b"]   # my roots\n')
        self.assertEqual(cc.apply(self.path, self.cfg)[0], "changed")
        new = self.path.read_text()
        self.assertIn("agents.max_depth = 2  # was one\n", new)
        self.assertIn('network_access = false # "quoted # not a comment"\n', new)     # the user's: untouched
        self.assertRegex(new, r'writable_roots = \["/a#b", .*\]   # my roots\n')
        self.assertEqual(tomllib.loads(new)["sandbox_workspace_write"]["writable_roots"][0], "/a#b")


PRE_FIX = """model = "m"

[sandbox_workspace_write]
network_access = true
writable_roots = ["/home/u/work", "{state}"]

[agents]
max_depth = 2
"""


class GrantTests(unittest.TestCase):
    """The swarm grants the Codex sandbox only the spool and marker dirs, never the state dir
    and never network access in the base table; an upgrade takes back what 0.1.0-pre granted."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="swarm-cc-"))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        p = mock.patch.dict(os.environ, {**home_env(self.home)}); p.start(); self.addCleanup(p.stop)
        self.path = self.home / ".codex/config.toml"
        self.path.parent.mkdir(parents=True)
        self.cfg = base_config()
        self.cfg["board"]["spool_dir"] = "~/.local/state/swarm/spool"
        self.state = str(self.home / ".local/state/swarm")

    def roots(self, cfg=None):
        return cc.required(cfg or self.cfg)[("sandbox_workspace_write", "writable_roots")]

    def test_upgrade_removes_the_old_shared_spool_root(self):
        # the pre-0.1.0 shared spool as a writable root is taken back too (bootstrap
        # --host codex and migrate)
        from swarm import bootstrap
        self.path.write_text(f'[sandbox_workspace_write]\nwritable_roots = ["{bootstrap.OLD_SPOOL}", "/keep/me"]\n')
        self.assertTrue(bootstrap._has_old_grants(self.path))
        status, detail = cc.remove_old_grants(self.path)
        self.assertEqual(status, "changed", detail)
        roots = tomllib.loads(self.path.read_text())["sandbox_workspace_write"]["writable_roots"]
        self.assertEqual(roots, ["/keep/me"])
        self.path.write_text(f'[sandbox_workspace_write]\nwritable_roots = ["{bootstrap.OLD_SPOOL}"]\n')
        cc.apply(self.path, self.cfg)
        roots = tomllib.loads(self.path.read_text())["sandbox_workspace_write"]["writable_roots"]
        self.assertNotIn(bootstrap.OLD_SPOOL, roots)

    def test_required_has_no_state_dir_and_no_network(self):
        req = cc.required(self.cfg)
        self.assertNotIn(("sandbox_workspace_write", "network_access"), req)
        self.assertEqual(self.roots(), [self.state + "/spool", self.state + "/active"])
        for root in self.roots():
            self.assertNotEqual(root, self.state)
            self.assertFalse(str(self.home / ".local/share/swarm").startswith(root))

    def test_board_dir_only_on_opt_in(self):
        cfg = base_config(backend="sqlite")
        cfg["board"]["spool_dir"] = "~/.local/state/swarm/spool"
        cfg["sqlite"]["path"] = "~/.local/share/swarm-board/board.sqlite3"
        self.assertNotIn(str(self.home / ".local/share/swarm-board"), self.roots(cfg))
        cfg["codex"]["board_writable"] = True
        self.assertIn(str(self.home / ".local/share/swarm-board"), self.roots(cfg))
        cfg["board"]["backend"] = "file"
        cfg["file"]["path"] = "~/b/board"
        self.assertIn(str(self.home / "b/board"), self.roots(cfg))
        cfg["board"]["backend"] = "postgres"
        self.assertEqual(len(self.roots(cfg)), 2)

    def test_fresh_apply_sets_no_network(self):
        self.assertEqual(cc.apply(self.path, self.cfg)[0], "changed")
        d = tomllib.loads(self.path.read_text())
        self.assertNotIn("network_access", d["sandbox_workspace_write"])
        self.assertFalse((self.path.parent / "swarm.config.toml").exists())

    def test_network_opt_in_goes_to_the_swarm_profile_not_the_base_table(self):
        self.cfg["codex"]["network_access"] = True
        status, detail = cc.apply(self.path, self.cfg)
        self.assertIn(status, ("changed", "manual"), detail)
        base = tomllib.loads(self.path.read_text())
        self.assertNotIn("network_access", base["sandbox_workspace_write"])
        prof = tomllib.loads((self.path.parent / "swarm.config.toml").read_text())
        self.assertIs(prof["sandbox_workspace_write"]["network_access"], True)
        self.assertIn("codex -p swarm", detail)
        self.assertNotIn("swarm", cc.profile_overrides(self.path.parent, base))    # ours: not a warning
        self.cfg["codex"]["network_access"] = False                                 # opt-out: ours removed
        cc.apply(self.path, self.cfg)
        self.assertFalse((self.path.parent / "swarm.config.toml").exists())

    def test_a_users_own_swarm_profile_is_never_overwritten(self):
        mine = self.path.parent / "swarm.config.toml"
        mine.write_text('model = "mine"\n')
        self.cfg["codex"]["network_access"] = True
        status, detail = cc.apply(self.path, self.cfg)
        self.assertEqual(status, "manual")
        self.assertEqual(mine.read_text(), 'model = "mine"\n')
        self.cfg["codex"]["network_access"] = False
        cc.apply(self.path, self.cfg)
        self.assertEqual(mine.read_text(), 'model = "mine"\n')

    def _pre_fix(self, backup_text):
        """A config.toml as 0.1.0-pre left it, with the backup it took before its first edit."""
        self.path.write_text(PRE_FIX.format(state=self.state))
        if backup_text is not None:
            (self.path.parent / "config.toml.pre-swarm-20260901-120000").write_text(backup_text)

    def test_upgrade_removes_old_grants(self):
        self._pre_fix('model = "m"\n[sandbox_workspace_write]\nwritable_roots = ["/home/u/work"]\n')
        status, detail = cc.apply(self.path, self.cfg)
        self.assertEqual(status, "changed", detail)
        d = tomllib.loads(self.path.read_text())
        sw = d["sandbox_workspace_write"]
        self.assertNotIn(self.state, sw["writable_roots"])
        self.assertNotIn("network_access", sw)
        self.assertEqual(sw["writable_roots"][0], "/home/u/work")
        self.assertIn(self.state + "/spool", sw["writable_roots"])
        self.assertIn(f"removed {self.state} from", detail)
        self.assertIn("removed network_access", detail)
        self.assertEqual(cc.apply(self.path, self.cfg)[0], "ok")          # done once

    def test_user_network_access_left_alone(self):
        self._pre_fix('[sandbox_workspace_write]\nnetwork_access = true\n')   # the user had it before the swarm
        status, detail = cc.apply(self.path, self.cfg)
        sw = tomllib.loads(self.path.read_text())["sandbox_workspace_write"]
        self.assertIs(sw["network_access"], True)
        self.assertNotIn(self.state, sw["writable_roots"])
        self.assertNotIn("removed network_access", detail)

    def test_no_backup_network_left_and_the_user_told(self):
        self._pre_fix(None)
        status, detail = cc.apply(self.path, self.cfg)
        self.assertEqual(status, "manual")
        self.assertIs(tomllib.loads(self.path.read_text())["sandbox_workspace_write"]["network_access"], True)
        self.assertIn("network_access", detail)

    def test_a_later_user_network_access_is_not_taken_again(self):
        self._pre_fix('[sandbox_workspace_write]\nwritable_roots = ["/home/u/work"]\n')
        cc.apply(self.path, self.cfg)
        text = self.path.read_text().replace("[sandbox_workspace_write]\n", "[sandbox_workspace_write]\nnetwork_access = true\n")
        self.path.write_text(text)                                              # the user sets it afterwards
        cc.apply(self.path, self.cfg)
        self.assertIs(tomllib.loads(self.path.read_text())["sandbox_workspace_write"]["network_access"], True)

    def test_remove_old_grants_alone(self):
        self._pre_fix('model = "m"\n')
        status, detail = cc.remove_old_grants(self.path)
        self.assertEqual(status, "changed")
        sw = tomllib.loads(self.path.read_text())["sandbox_workspace_write"]
        self.assertEqual(sw["writable_roots"], ["/home/u/work"])
        self.assertNotIn("network_access", sw)
        self.assertEqual(cc.remove_old_grants(self.path)[0], "ok")
