"""install.sh --channel/--main/--ref: a tagged git repo fixture (v0.1.1, v0.1.2, then one more
commit), stub claude/codex CLIs that record their arguments and "install" what they were pointed at."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from support import ROOT
from test_channel import git, make_repo

STUB = r'''#!/bin/sh
echo "$@" >> "$LOGDIR/$(basename "$0").args"
case "$1 $2 $3" in
  "plugin marketplace add")
    if [ -f "$LOGDIR/mp" ]; then exit 1; fi
    echo "$4" > "$LOGDIR/mp"; [ "$5" = "--ref" ] && echo "$6" >> "$LOGDIR/mp"; exit 0 ;;
  "plugin marketplace remove") rm -f "$LOGDIR/mp"; exit 0 ;;
  "plugin marketplace update"|"plugin marketplace upgrade") [ -f "$LOGDIR/mp" ] && exit 0 || exit 1 ;;
esac
case "$1 $2" in
  "plugin install"|"plugin add")
    src="$(head -n1 "$LOGDIR/mp")"; ref=""
    case "$src" in *"#"*) ref="${src#*#}"; src="${src%%#*}";; esac
    [ -n "$(sed -n 2p "$LOGDIR/mp")" ] && ref="$(sed -n 2p "$LOGDIR/mp")"
    rm -rf "$LOGDIR/tree"; git clone -q "$src" "$LOGDIR/tree"
    [ -n "$ref" ] && git -C "$LOGDIR/tree" checkout -q "$ref"
    mkdir -p "$HOME/.claude/plugins"
    printf '{"plugins": {"swarm@swarm": [{"installPath": "%s"}]}}' "$LOGDIR/tree" > "$HOME/.claude/plugins/installed_plugins.json"
    touch "$LOGDIR/installed"; exit 0 ;;
  "plugin list") [ -f "$LOGDIR/installed" ] && echo "swarm@swarm enabled"; exit 0 ;;
esac
[ "$1" = "--version" ] && echo "stub 9.9.9"
exit 0
'''


class InstallChannelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="swarm-instchan-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = make_repo(self.tmp)
        self.url = "file://" + str(self.repo)
        self.home = self.tmp / "home"; self.home.mkdir()
        self.logs = self.tmp / "logs"; self.logs.mkdir()
        self.bindir = self.tmp / "bin"; self.bindir.mkdir()
        for name in ("claude", "codex"):
            (self.bindir / name).write_text(STUB)
            (self.bindir / name).chmod(0o755)
        gitdir = str(Path(shutil.which("git")).parent)
        sysbin = "/usr/bin:/bin"
        self.env = {"HOME": str(self.home), "LOGDIR": str(self.logs), "TMPDIR": str(self.tmp),
                    "PATH": f"{self.bindir}:{Path(sys.executable).parent}:{gitdir}:{sysbin}",
                    "SWARM_CONFIG": str(self.home / "c.toml")}

    def install(self, *flags):
        res = subprocess.run(["bash", str(ROOT / "install.sh"), "--yes", "--no-color", "--current-user",
                              "--host", "claude", "--marketplace", self.url, *flags],
                             capture_output=True, text=True, timeout=120, env=self.env, stdin=subprocess.DEVNULL)
        return res.returncode, res.stdout + res.stderr

    def claude_args(self):
        return (self.logs / "claude.args").read_text()

    def test_default_is_the_newest_release_tag(self):
        rc, out = self.install()
        self.assertEqual(rc, 0, out)
        self.assertIn(f"plugin marketplace add {self.url}#v0.1.2", self.claude_args())
        self.assertIn("channel: release (newest tag v0.1.2)", out)
        self.assertIn("installed swarm 0.1.2: channel release, ref v0.1.2", out)

    def test_main_installs_the_tip(self):
        for flags in (("--main",), ("--channel", "main"), ("--channel=main",)):
            shutil.rmtree(self.logs); self.logs.mkdir()
            rc, out = self.install(*flags)
            self.assertEqual(rc, 0, out)
            self.assertIn(f"plugin marketplace add {self.url}\n", self.claude_args())
            self.assertNotIn("#", self.claude_args())
            self.assertIn("installed swarm 0.1.3-dev: channel main", out)

    def test_explicit_ref_overrides_the_channel(self):
        rc, out = self.install("--main", "--ref", "v0.1.1")
        self.assertEqual(rc, 0, out)
        self.assertIn(f"{self.url}#v0.1.1", self.claude_args())
        self.assertIn("installed swarm 0.1.1", out)

    def test_rerun_moves_an_existing_marketplace_to_the_new_ref(self):
        rc, out = self.install("--ref", "v0.1.1")
        self.assertEqual(rc, 0, out)
        rc, out = self.install()
        self.assertEqual(rc, 0, out)
        args = self.claude_args()
        self.assertIn("plugin marketplace remove swarm", args)
        self.assertIn("installed swarm 0.1.2", out)

    def test_no_tag_falls_back_to_main_with_a_warning(self):
        repo2 = self.tmp / "notags"
        shutil.copytree(self.repo, repo2)
        for t in subprocess.run(["git", "tag"], cwd=repo2, capture_output=True, text=True).stdout.split():
            git(repo2, "tag", "-d", t)
        self.url = "file://" + str(repo2)
        rc, out = self.install()
        self.assertEqual(rc, 0, out)
        self.assertIn("falling back to the tip of main", out)
        self.assertNotIn("#", self.claude_args())

    def test_codex_gets_ref_flag(self):
        res = subprocess.run(["bash", str(ROOT / "install.sh"), "--yes", "--no-color", "--current-user",
                              "--host", "codex", "--marketplace", self.url],
                             capture_output=True, text=True, timeout=120, env=self.env, stdin=subprocess.DEVNULL)
        self.assertIn(f"plugin marketplace add {self.url} --ref v0.1.2", (self.logs / "codex.args").read_text(),
                      res.stdout + res.stderr)

    def test_bad_channel_is_refused(self):
        rc, out = self.install("--channel", "nightly")
        self.assertNotEqual(rc, 0)
        self.assertIn("--channel must be release or main", out)


if __name__ == "__main__":
    unittest.main()
