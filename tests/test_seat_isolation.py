"""Task 20: drive seats run without the person's push credentials (swarm.seatjail, supervisor.seathome)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

import support  # noqa: F401
from swarm import bootstrap, seatjail
from swarm.supervisor import runner, seathome

JAIL = str(Path(seatjail.__file__).resolve())
CAN_JAIL = seatjail.available()[0] if sys.platform.startswith("linux") else False


class ScrubEnvTest(unittest.TestCase):
    def test_push_credential_variables_are_dropped(self):
        env = {"PATH": "/bin", "HOME": "/h", "SSH_AUTH_SOCK": "/s", "GH_TOKEN": "x", "GITHUB_TOKEN": "x",
               "GIT_ASKPASS": "/a", "SSH_ASKPASS": "/a", "GIT_SSH_COMMAND": "ssh -i k", "GITEA_TOKEN": "x",
               "MY_SERVICE_TOKEN": "x", "GIT_CONFIG_KEY_0": "credential.helper", "GIT_CONFIG_VALUE_0": "store",
               "GIT_CONFIG_COUNT": "1", "DBUS_SESSION_BUS_ADDRESS": "unix:x", "XDG_RUNTIME_DIR": "/run/user/1"}
        self.assertEqual(seatjail.scrub_env(env), {"PATH": "/bin", "HOME": "/h"})

    def test_harness_auth_and_swarm_run_variables_are_kept(self):
        env = {"SWARM_RESUME_TOKEN": "t", "SWARM_RUN_ID": "r", "CLAUDE_CODE_OAUTH_TOKEN": "c",
               "ANTHROPIC_API_KEY": "k", "CODEX_HOME": "/c", "OPENAI_API_KEY": "o"}
        self.assertEqual(seatjail.scrub_env(env), env)

    def test_seat_env_points_git_and_gh_at_the_seat(self):
        env = seatjail.seat_env({"GH_TOKEN": "x", "PATH": "/bin"},
                                {"gitconfig": "/s/gitconfig", "gh_config_dir": "/s/gh", "ssh_config": "/h/.ssh/config"})
        self.assertNotIn("GH_TOKEN", env)
        self.assertEqual(env["GIT_CONFIG_GLOBAL"], "/s/gitconfig")
        self.assertEqual(env["GH_CONFIG_DIR"], "/s/gh")
        self.assertEqual(env["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(env["GIT_SSH_COMMAND"], "ssh -F /h/.ssh/config")
        self.assertEqual(env[seatjail.JAIL_ENV], "1")


class ChildEnvTest(unittest.TestCase):
    def test_a_drive_seat_gets_no_credential_even_through_pass_env(self):
        with mock.patch.dict(os.environ, {"GH_TOKEN": "x", "SSH_AUTH_SOCK": "/s", "GITHUB_TOKEN": "y"}):
            env = runner._child_env(pass_env=("GH_TOKEN", "SSH_AUTH_SOCK", "GITHUB_TOKEN"), drive=True)
        for k in ("GH_TOKEN", "SSH_AUTH_SOCK", "GITHUB_TOKEN"):
            self.assertNotIn(k, env)

    def test_a_classic_replacement_keeps_what_the_person_passed(self):
        with mock.patch.dict(os.environ, {"GH_TOKEN": "x"}):
            env = runner._child_env(pass_env=("GH_TOKEN",))
        self.assertEqual(env.get("GH_TOKEN"), "x")


class SeatHomeCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        (self.home / ".ssh").mkdir(parents=True)
        (self.home / ".ssh" / "known_hosts").write_text("github.com ssh-ed25519 AAAA\n")
        (self.home / ".ssh" / "id_ed25519").write_text("not a real key\n")
        priv = self.tmp / "private"
        priv.mkdir(mode=0o700)
        for p in (mock.patch("swarm.paths.home", lambda: self.home),
                  mock.patch("swarm.supervisor.settings.private_dir", lambda: priv),
                  mock.patch("swarm.supervisor.seathome.pinned_paths", lambda cfg: [])):
            p.start()
            self.addCleanup(p.stop)
        self.cfg = {"drive": {"seat_git_name": "Seat", "seat_git_email": "seat@example.test"}}


class PrepareTest(SeatHomeCase):
    def test_the_seat_home_holds_no_key_and_no_token(self):
        h = seathome.prepare(self.cfg, "J", "eng-1")
        self.assertEqual(os.stat(h).st_mode & 0o777, 0o700)
        self.assertEqual(sorted(os.listdir(h / ".ssh")), ["config", "known_hosts"])
        self.assertEqual(os.listdir(h / "gh"), [])
        self.assertIn("github.com", (h / ".ssh" / "known_hosts").read_text())
        ssh = (h / ".ssh" / "config").read_text()
        self.assertIn("IdentityAgent none", ssh)
        self.assertIn("IdentityFile /dev/null", ssh)
        git = (h / "gitconfig").read_text()
        self.assertIn('name = "Seat"', git)
        self.assertIn("[credential]\n\thelper =\n", git)
        self.assertIn("insteadOf = git@github.com:", git)

    def test_a_planted_link_in_the_seat_home_is_replaced_not_followed(self):
        h = seathome.prepare(self.cfg, "J", "eng-1")
        target = self.tmp / "victim"
        target.write_text("keep")
        (h / "gitconfig").unlink()
        (h / "gitconfig").symlink_to(target)
        seathome.prepare(self.cfg, "J", "eng-1")
        self.assertEqual(target.read_text(), "keep")
        self.assertFalse((h / "gitconfig").is_symlink())

    def test_a_relative_seat_key_is_refused(self):
        self.cfg["drive"]["seat_ssh_key"] = "keys/ro"
        with self.assertRaises(seathome.SeatIsolationError):
            seathome.prepare(self.cfg, "J", "eng-1")

    def test_a_read_only_deploy_key_is_the_only_identity(self):
        self.cfg["drive"]["seat_ssh_key"] = "/keys/ro-deploy"
        h = seathome.prepare(self.cfg, "J", "eng-1")
        self.assertIn('IdentityFile "/keys/ro-deploy"', (h / ".ssh" / "config").read_text())


class SpecTest(SeatHomeCase):
    def test_the_person_ssh_dir_is_replaced_and_credentials_covered(self):
        h = seathome.prepare(self.cfg, "J", "s")
        spec = seathome.jail_spec(self.cfg, h, self.home)
        self.assertEqual(spec["binds"], [[str(h / ".ssh"), str(self.home / ".ssh")]])
        for rel in (".config/gh", ".git-credentials", ".netrc", ".gitconfig"):
            self.assertIn(str(self.home / rel), spec["cover"])

    def test_pinned_credentials_are_covered(self):
        owner = {"credentials": {"github": "ssh-key:/k/id", "gitea": "ssh-agent:/run/agent.sock",
                                 "h": "helper:/usr/bin/gh auth git-credential"},
                 "config_dirs": {"h": "/cfg/gh"}}
        self.assertEqual(sorted(seathome._pin_paths(owner)),
                         ["/cfg/gh", "/k/id", "/k/id.pub", "/run/agent.sock"])

    def test_extra_cover_paths_from_config(self):
        self.cfg["drive"]["seat_cover_paths"] = ["~/vault"]
        self.assertIn(os.path.expanduser("~/vault"), seathome.cover_paths(self.cfg, self.home))

    def test_platform_lists(self):
        self.assertIn("Library/Application Support/gh", seathome.platform_cover("darwin"))
        self.assertIn("AppData/Roaming/GitHub CLI", seathome.platform_cover("win32"))
        self.assertEqual(seathome.platform_cover("linux"), seathome.HOME_COVER)


class WrapTest(SeatHomeCase):
    def test_wrapped_argv(self):
        argv = seathome.wrap(self.cfg, ["claude", "-p"], job="J", seat="s", python="/py", probe=lambda: (True, ""))
        self.assertEqual(argv[:3], ["/py", "-I", seathome.jail_script()])
        self.assertEqual(argv[4:], ["--", "claude", "-p"])
        self.assertIn("binds", json.loads(argv[3]))

    def test_a_host_that_cannot_jail_refuses_the_seat(self):
        with self.assertRaises(seathome.SeatIsolationError) as cm:
            seathome.wrap(self.cfg, ["claude"], job="J", seat="s", probe=lambda: (False, "no userns"))
        self.assertIn("no userns", str(cm.exception))

    def test_off_is_the_person_opt_out(self):
        self.cfg["drive"]["seat_isolation"] = "off"
        self.assertEqual(seathome.wrap(self.cfg, ["claude"], job="J", seat="s",
                                       probe=lambda: (False, "x")), ["claude"])

    def test_a_bad_mode_is_refused(self):
        self.cfg["drive"]["seat_isolation"] = "maybe"
        with self.assertRaises(seathome.SeatIsolationError):
            seathome.wrap(self.cfg, ["claude"], job="J", seat="s", probe=lambda: (True, ""))

    def test_runner_wraps_drive_seats_only(self):
        run = {"argv": ["claude", "-p"], "job": "J", "name": "Lisa", "seat": "eng"}
        with mock.patch("swarm.seatjail.available", lambda: (True, "")):
            self.assertEqual(runner._seat_argv(self.cfg, run), (["claude", "-p"], None))
            argv, why = runner._seat_argv(self.cfg, {**run, "launch_id": "L1"})
        self.assertIsNone(why)
        self.assertEqual(argv[-3:], ["--", "claude", "-p"])
        with mock.patch("swarm.seatjail.available", lambda: (False, "no userns")):
            _, why = runner._seat_argv(self.cfg, {**run, "launch_id": "L1"})
        self.assertIn("no userns", why)


class DoctorTest(unittest.TestCase):
    def test_lines(self):
        ok = bootstrap._seat_isolation_check({}, probe=lambda c: (True, "active: clean"))
        self.assertTrue(ok.ok)
        leak = bootstrap._seat_isolation_check({}, probe=lambda c: (False, "KEY /h/.ssh/id_ed25519"))
        self.assertFalse(leak.ok)
        self.assertIn("can reach a credential", leak.detail)
        off = bootstrap._seat_isolation_check({}, probe=lambda c: (None, "off ([drive] seat_isolation)"))
        self.assertIsNone(off.ok)
        na = bootstrap._seat_isolation_check({}, probe=lambda c: (None, "unavailable: EPERM"))
        self.assertFalse(na.ok)
        self.assertIn("NOT started", na.detail)

    def test_the_probe_reports_what_a_seat_could_reach(self):
        """The probe script itself, run unjailed on a fake home: it names the key, the listable gh dir and
        the token variable (so a clean answer in the jail means something)."""
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / ".ssh").mkdir()
        (tmp / ".ssh" / "id_ed25519").write_text("x")
        (tmp / "gh").mkdir()
        (tmp / "gh" / "hosts.yml").write_text("x")
        env = {"PATH": os.environ["PATH"], "HOME": str(tmp), "GH_TOKEN": "x", "GIT_CONFIG_NOSYSTEM": "1",
               "GIT_CONFIG_GLOBAL": os.devnull, "GH_CONFIG_DIR": str(tmp / "nogh")}
        r = subprocess.run(["sh", "-c", seathome.PROBE, "probe", str(tmp / "gh")], capture_output=True,
                           text=True, env=env, timeout=60)
        self.assertEqual(r.returncode, 1)
        self.assertIn("LISTABLE", r.stdout)
        self.assertIn("KEY", r.stdout)
        self.assertIn("ENV", r.stdout)


FAKE_HARNESS = textwrap.dedent("""\
    import json, os, subprocess, sys
    home, bare = sys.argv[1], sys.argv[2]
    out = {"uid": os.getuid(), "pid": os.getpid(),
           "ssh": sorted(os.listdir(os.path.join(home, ".ssh"))),
           "gh": sorted(os.listdir(os.path.join(home, "gh"))),
           "netrc": open(os.path.join(home, ".netrc")).read(),
           "env": sorted(k for k in os.environ if k in ("GH_TOKEN", "SSH_AUTH_SOCK", "GITHUB_TOKEN")),
           "jail": os.environ.get("SWARM_SEAT_JAIL")}
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    out["umount"] = libc.umount2(os.path.join(home, "gh").encode(), 2)
    r = subprocess.run(["git", "-C", os.path.join(home, "work"), "push", "file://" + bare, "HEAD:refs/heads/x"],
                       capture_output=True, text=True)
    out["push"] = r.returncode
    print(json.dumps(out))
    """)


@unittest.skipUnless(CAN_JAIL, "needs unprivileged user namespaces (Linux)")
class JailTest(unittest.TestCase):
    """The real jail, on a fake home: a fake harness started in it sees no credential and cannot uncover one."""

    def test_fake_harness_in_the_jail(self):
        tmp = Path(tempfile.mkdtemp(dir=os.environ.get("SWARM_TEST_TMP")))
        self.addCleanup(shutil.rmtree, tmp, True)
        home = tmp / "home"
        for d in (".ssh", "gh", "seat-ssh", "creds"):
            (home / d).mkdir(parents=True)
        (home / ".ssh" / "id_ed25519").write_text("not a real key")
        (home / "seat-ssh" / "known_hosts").write_text("kh")
        (home / "gh" / "hosts.yml").write_text("oauth_token: x")
        (home / ".netrc").write_text("machine github.com password x")
        bare = home / "creds" / "dest.git"      # a destination only a credential reaches
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
        subprocess.run(git + ["init", "-q", "--bare", str(bare)], check=True)
        subprocess.run(git + ["init", "-q", str(home / "work")], check=True)
        subprocess.run(git + ["-C", str(home / "work"), "commit", "-q", "--allow-empty", "-m", "x"], check=True)
        (tmp / "harness.py").write_text(FAKE_HARNESS)
        spec = {"binds": [[str(home / "seat-ssh"), str(home / ".ssh")]],
                "cover": [str(home / "gh"), str(home / ".netrc"), str(home / "creds")]}
        env = {**os.environ, "GH_TOKEN": "x", "SSH_AUTH_SOCK": "/nowhere", "GITHUB_TOKEN": "y"}
        r = subprocess.run([sys.executable, "-I", JAIL, json.dumps(spec), "--", sys.executable, "-I",
                            str(tmp / "harness.py"), str(home), str(bare)],
                           capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out["uid"], os.getuid())
        self.assertEqual(out["pid"], 2)               # its own pid namespace (init is 1)
        self.assertEqual(out["ssh"], ["known_hosts"])
        self.assertEqual(out["gh"], [])
        self.assertEqual(out["netrc"], "")
        self.assertEqual(out["env"], [])
        self.assertEqual(out["jail"], "1")
        self.assertEqual(out["umount"], -1)           # the covers are locked
        self.assertNotEqual(out["push"], 0)           # the destination behind the credential is unreachable
        self.assertEqual((home / ".ssh" / "id_ed25519").read_text(), "not a real key")   # untouched outside

    def test_exit_status_is_passed_through(self):
        r = subprocess.run([sys.executable, "-I", JAIL, "{}", "--", "sh", "-c", "exit 7"], timeout=60)
        self.assertEqual(r.returncode, 7)


class UnavailableTest(unittest.TestCase):
    def test_no_namespaces_means_nothing_runs(self):
        with mock.patch.object(seatjail, "_unshare", side_effect=OSError(1, "unshare: EPERM")), \
                mock.patch("os.execvpe") as ex, mock.patch("os.fork") as fork:
            self.assertEqual(seatjail.run({}, ["true"]), seatjail.EXIT_UNAVAILABLE)
        ex.assert_not_called()
        fork.assert_not_called()


if __name__ == "__main__":
    unittest.main()
