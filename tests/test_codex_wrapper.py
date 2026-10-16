"""[harness.codex] wrapper = true: codex runs through an argv prefix (another OS user over ssh), brief on stdin,
no local jail."""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import base_config, posix_only

from swarm import bootstrap
from swarm.supervisor import launch, runner

WRAPPER = ["ssh", "root@node", "pct", "exec", "118", "--", "runuser", "-u", "codex", "--", "codex"]
BIN = os.path.abspath(os.path.join(os.sep, "usr", "bin"))   # an absolute directory on every OS (D:\usr\bin on Windows)
ABS_SSH = os.path.join(BIN, "ssh")


def cfg(command=None, wrapper=True, **extra):
    c = base_config()
    c["supervise"] = {"enabled": True}
    c["harness"] = {"codex": {k: v for k, v in {"command": command, "wrapper": wrapper, **extra}.items()
                              if v is not None}}
    return c


class WrapperArgvTests(unittest.TestCase):
    def spec(self, c, **kw):
        with mock.patch.object(launch.shutil, "which", side_effect=lambda p: p if os.path.isabs(p) else os.path.join(BIN, p)):
            return launch.codex_spec(c, prompt="the brief", workdir="/w", model="gpt-6-astra", minutes=1,
                                     budget_usd=1, drive=True, **kw)

    def test_argv_is_the_wrapper_then_codex_args_and_the_brief_is_stdin(self):
        s = self.spec(cfg(WRAPPER))
        self.assertEqual(list(s.argv[:len(WRAPPER)]), [ABS_SSH, *WRAPPER[1:]])
        self.assertEqual(s.argv[len(WRAPPER):len(WRAPPER) + 2], ("exec", "--json"))
        self.assertIn("gpt-6-astra", s.argv)
        self.assertEqual(s.argv[-1], "-")                     # codex reads its prompt from stdin
        self.assertEqual(s.stdin, "the brief")
        self.assertNotIn("the brief", s.argv)
        self.assertNotIn("/w", s.argv)                        # no local path the remote side cannot see

    def test_string_command_is_split_as_shell_words(self):
        s = self.spec(cfg("ssh root@node codex"))
        self.assertEqual(list(s.argv[:3]), [ABS_SSH, "root@node", "codex"])

    def test_resume_goes_through_the_wrapper_too(self):
        s = self.spec(cfg(WRAPPER), resume="thr_1")
        self.assertEqual(s.argv[len(WRAPPER):len(WRAPPER) + 2], ("exec", "resume"))
        self.assertEqual(s.argv[-2:], ("thr_1", "-"))

    def test_invalid_values_are_refused(self):
        for bad in ({"command": 5}, {"command": []}, {"command": ["ssh", ""]}, {"command": ""}):
            c = cfg(wrapper=True)
            c["harness"]["codex"].update(bad)
            with self.assertRaisesRegex(ValueError, r"\[harness.codex\] command must be a path or argv"):
                launch.codex_command(c)
        for bad in ("yes", 1):
            with self.assertRaisesRegex(ValueError, "wrapper must be true or false"):
                launch.codex_command(cfg(WRAPPER, wrapper=bad))
        with self.assertRaisesRegex(ValueError, "wrapper = true needs a command"):
            launch.codex_command(cfg(None))
        for rel in ("./ssh", "bin/ssh", "../ssh"):
            with self.assertRaisesRegex(ValueError, "absolute path or a program on PATH"):
                launch.codex_command(cfg([rel, "codex"]))

    def test_an_absolute_or_bare_first_word_is_accepted(self):
        self.assertEqual(launch.codex_command(cfg([ABS_SSH, "h", "codex"])), [ABS_SSH, "h", "codex"])
        self.assertEqual(launch.codex_command(cfg(WRAPPER)), WRAPPER)

    def test_without_the_flag_a_relative_command_is_as_before(self):
        self.assertEqual(launch.codex_command(cfg(["./codex"], wrapper=None)), ["./codex"])


class WrapperRunTests(unittest.TestCase):
    RUN = {"argv": ["ssh", "h", "codex", "exec", "-"], "job": "J", "name": "Lisa", "launch": "rev",
           "launch_id": "L1", "cwd": "/w", "harness": "codex"}

    def test_a_wrapper_launch_is_not_jailed_and_logs_once(self):
        runner._WRAPPER_LOGGED = False
        self.addCleanup(setattr, runner, "_WRAPPER_LOGGED", False)
        with mock.patch("swarm.supervisor.launchhome.launch", side_effect=AssertionError("jailed")), \
                mock.patch("swarm.supervisor.settings.log") as log:
            for _ in range(2):
                run = dict(self.RUN)
                self.assertEqual(runner._launch_argv(cfg(WRAPPER), run), (self.RUN["argv"], None))
                self.assertEqual(run["launch_mode"], "off")
                self.assertNotIn("launch_home", run)
        self.assertEqual(log.call_count, 1)
        self.assertIn("not jailed", log.call_args.args[0])

    def test_a_normal_codex_launch_and_a_claude_launch_are_still_jailed(self):
        jailed = (["jail", "--", "x"], None, "required")
        for harness, c in (("codex", cfg("/usr/bin/codex", wrapper=None)), ("claude", cfg(WRAPPER))):
            with mock.patch("swarm.supervisor.launchhome.launch", return_value=jailed) as jail, \
                    mock.patch("swarm.supervisor.launchhome.home_dir", return_value=Path("/h")):
                argv, why = runner._launch_argv(c, {**self.RUN, "harness": harness})
            jail.assert_called_once()
            self.assertEqual((argv, why), (["jail", "--", "x"], None))

    def test_an_invalid_wrapper_flag_refuses_the_launch(self):
        argv, why = runner._launch_argv(cfg(WRAPPER, wrapper="yes"), dict(self.RUN))
        self.assertIn("wrapper must be true or false", why)

    @posix_only("runs a POSIX sh script")
    def test_a_fake_wrapper_gets_codex_args_and_the_brief_on_stdin(self):
        with tempfile.TemporaryDirectory(prefix="swarm-wrapper-") as d:
            script, out = Path(d) / "wrapper", Path(d) / "seen.json"
            script.write_text(f"#!{sys.executable}\nimport json, sys\n"
                              f"json.dump({{'argv': sys.argv[1:], 'stdin': sys.stdin.read()}}, open({str(out)!r}, 'w'))\n")
            script.chmod(script.stat().st_mode | stat.S_IXUSR)
            c = cfg([str(script), "--host", "ct118", "codex"])
            s = launch.codex_spec(c, prompt="the brief\nline 2", workdir=d, model="m", minutes=1,
                                  budget_usd=1, drive=True)
            argv, why = runner._launch_argv(c, {**WrapperRunTests.RUN, "argv": list(s.argv), "cwd": d})
            self.assertIsNone(why)
            subprocess.run(argv, input=s.stdin, text=True, check=True, timeout=30)
            seen = json.loads(out.read_text())
        self.assertEqual(seen["stdin"], "the brief\nline 2")
        self.assertEqual(seen["argv"][:3], ["--host", "ct118", "codex"])
        self.assertEqual(seen["argv"][3:5], ["exec", "--json"])
        self.assertEqual(seen["argv"][-1], "-")


class WrapperDoctorTests(unittest.TestCase):
    def test_warn_names_the_trust_boundary(self):
        (c,) = bootstrap._codex_wrapper_checks(cfg(WRAPPER))
        self.assertEqual((c.name, c.ok), ("codex wrapper", None))
        self.assertIn("NOT jailed", c.detail)
        self.assertIn("trust boundary", c.detail)
        self.assertIn("runuser", c.detail)

    def test_nothing_without_a_wrapper_and_a_fail_for_a_bad_one(self):
        self.assertEqual(bootstrap._codex_wrapper_checks(cfg("/usr/bin/codex", wrapper=None)), [])
        (c,) = bootstrap._codex_wrapper_checks(cfg(None))
        self.assertIs(c.ok, False)


if __name__ == "__main__":
    unittest.main()
