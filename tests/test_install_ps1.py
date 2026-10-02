"""install.ps1 (the PowerShell installer) against a tagged git fixture and stub claude/codex CLIs.
Needs PowerShell and the Windows .cmd launcher, so it runs on the Windows CI runner only; the
parse check below runs wherever pwsh exists."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from support import ROOT
from test_channel import make_repo

PWSH = shutil.which("pwsh") or shutil.which("powershell")

STUB_PY = r'''
import os, subprocess, sys, json, shutil
log = os.environ["LOGDIR"]; name = sys.argv[1]
args = sys.argv[2:]
open(os.path.join(log, name + ".args"), "a").write(" ".join(args) + "\n")
mp = os.path.join(log, "mp")
if args[:3] == ["plugin", "marketplace", "add"]:
    if os.path.exists(mp): sys.exit(1)
    src = args[3]; ref = ""
    if "#" in src: src, ref = src.split("#", 1)
    if "--ref" in args: ref = args[args.index("--ref") + 1]
    open(mp, "w").write(src + "\n" + ref + "\n"); sys.exit(0)
if args[:3] == ["plugin", "marketplace", "remove"]:
    if os.path.exists(mp): os.remove(mp)
    sys.exit(0)
if args[:3] in (["plugin", "marketplace", "update"], ["plugin", "marketplace", "upgrade"]):
    sys.exit(0 if os.path.exists(mp) else 1)
if args[:2] in (["plugin", "install"], ["plugin", "add"]):
    src, ref = open(mp).read().split("\n")[:2]
    tree = os.path.join(log, "tree"); shutil.rmtree(tree, ignore_errors=True)
    subprocess.run(["git", "clone", "-q", src, tree], check=True)
    if ref: subprocess.run(["git", "-C", tree, "checkout", "-q", ref], check=True)
    d = os.path.join(os.path.expanduser("~"), ".claude", "plugins"); os.makedirs(d, exist_ok=True)
    json.dump({"plugins": {"swarm@swarm": [{"installPath": tree}]}}, open(os.path.join(d, "installed_plugins.json"), "w"))
    open(os.path.join(log, "installed"), "w").write("x"); sys.exit(0)
if args[:2] == ["plugin", "list"]:
    if os.path.exists(os.path.join(log, "installed")): print("swarm@swarm enabled")
    sys.exit(0)
if args[:1] == ["--version"]: print("stub 9.9.9")
'''


@unittest.skipUnless(PWSH, "PowerShell not available")
class InstallPs1Tests(unittest.TestCase):
    def test_script_parses(self):
        res = subprocess.run([PWSH, "-NoProfile", "-Command",
                              "$e=$null; [void][System.Management.Automation.Language.Parser]::ParseFile("
                              f"'{ROOT / 'install.ps1'}',[ref]$null,[ref]$e); if($e){{$e|%{{$_.Message}}; exit 1}}"],
                             capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)

    @unittest.skipUnless(sys.platform == "win32", "uses .cmd stub CLIs")
    def test_default_installs_the_newest_tag_and_main_the_tip(self):
        tmp = Path(tempfile.mkdtemp(prefix="swarm-ps1-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        repo = make_repo(tmp)
        home = tmp / "home"; home.mkdir()
        bindir = tmp / "bin"; bindir.mkdir()
        (bindir / "stub.py").write_text(STUB_PY)
        (bindir / "claude.cmd").write_text(f'@echo off\r\n"{sys.executable}" "{bindir / "stub.py"}" claude %*\r\n')
        url = repo.as_uri()
        for n, (flags, want) in enumerate((([], "installed swarm"), (["-Main"], "channel main"))):
            logs = tmp / f"logs{n}"; logs.mkdir()   # a fresh one each time (git's read-only files resist rmtree)
            env = {**os.environ, "USERPROFILE": str(home), "HOME": str(home), "LOGDIR": str(logs),
                   "PATH": f"{bindir};{os.environ['PATH']}", "SWARM_CONFIG": str(home / "c.toml")}
            res = subprocess.run([PWSH, "-NoProfile", "-File", str(ROOT / "install.ps1"), "-Target", "claude",
                                  "-Marketplace", url, "-Yes", "-NoColor", "-NoPath", *flags],
                                 capture_output=True, text=True, env=env, timeout=300)
            out = res.stdout + res.stderr
            self.assertEqual(res.returncode, 0, out)
            self.assertIn(want, out, out)
            args = (logs / "claude.args").read_text()
            if flags:
                self.assertNotIn("#", args)
            else:
                self.assertIn(f"{url}#v0.1.2", args)


if __name__ == "__main__":
    unittest.main()
