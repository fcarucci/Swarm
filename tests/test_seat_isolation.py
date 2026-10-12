"""Task 20: drive seats run in a jail with an allowlisted file view and no board access of their own
(swarm.seatjail, supervisor.seathome, supervisor.seatbroker, swarm.seatbroker_client)."""
from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
import uuid
from pathlib import Path
from unittest import mock

import support  # noqa: F401
from swarm import bootstrap, person, seatbroker_client, seatjail
from swarm.supervisor import runner, seatbroker, seathome

LIB = Path(seatjail.__file__).resolve().parent.parent
JAIL = str(Path(seatjail.__file__).resolve())
CAN_JAIL = seatjail.available()[0] if sys.platform.startswith("linux") else False


def tmpdir(test) -> Path:
    d = Path(tempfile.mkdtemp(dir=os.environ.get("SWARM_TEST_TMP")))
    test.addCleanup(shutil.rmtree, d, True)
    return d


class ScrubEnvTest(unittest.TestCase):
    def test_only_the_allowlist_passes(self):
        env = {"PATH": "/bin", "HOME": "/h", "LANG": "C", "LC_ALL": "C", "SSH_AUTH_SOCK": "/s", "GH_TOKEN": "x",
               "PGPASSWORD": "x", "PGPASSFILE": "/p", "DATABASE_URL": "x", "AWS_ACCESS_KEY_ID": "x",
               "AWS_SECRET_ACCESS_KEY": "x", "TMUX": "/t", "HERDR_SOCKET_PATH": "/h", "KUBECONFIG": "/k",
               "DOCKER_CONFIG": "/d", "LD_PRELOAD": "x", "BASH_ENV": "x", "SWARM_GH_TOKEN": "x",
               "SWARM_DB_PASSWORD": "x", "CLAUDE_GITHUB_TOKEN": "x", "CODEX_GH_TOKEN": "x", "GPG_AGENT_INFO": "x",
               "CLAUDE_CONFIG_DIR": "/c", "CODEX_HOME": "/c", "SWARM_CONFIG": "/c"}
        self.assertEqual(seatjail.scrub_env(env), {"PATH": "/bin", "HOME": "/h", "LANG": "C", "LC_ALL": "C"})

    def test_harness_auth_and_run_variables_are_kept(self):
        env = {"SWARM_RESUME_TOKEN": "t", "SWARM_RUN_ID": "r", "CLAUDE_CODE_OAUTH_TOKEN": "c",
               "ANTHROPIC_API_KEY": "k", "OPENAI_API_KEY": "o"}
        self.assertEqual(seatjail.scrub_env(env), env)

    def test_seat_env(self):
        env = seatjail.seat_env({"GH_TOKEN": "x", "PATH": "/bin"},
                                {"env": {"GIT_CONFIG_GLOBAL": "/h/.gitconfig", "SWARM_SEAT_BROKER": "/h/b"},
                                 "path_prepend": ["/h/.swarm-seat/bin"], "home": "/h"})
        self.assertNotIn("GH_TOKEN", env)
        self.assertEqual(env["PATH"], "/h/.swarm-seat/bin:/bin")
        self.assertEqual(env["GIT_CONFIG_GLOBAL"], "/h/.gitconfig")
        self.assertEqual(env["TMPDIR"], "/tmp")
        self.assertEqual(env[seatjail.JAIL_ENV], "1")

    def test_runner_scrubs_a_drive_seat_even_through_pass_env(self):
        with mock.patch.dict(os.environ, {"GH_TOKEN": "x", "PGPASSWORD": "y", "TMUX": "z"}):
            env = runner._child_env(pass_env=("GH_TOKEN", "PGPASSWORD", "TMUX"), drive=True)
            classic = runner._child_env(pass_env=("GH_TOKEN",))
        for k in ("GH_TOKEN", "PGPASSWORD", "TMUX"):
            self.assertNotIn(k, env)
        self.assertEqual(classic.get("GH_TOKEN"), "x")


class SeatHomeCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tmpdir(self)
        self.home = self.tmp / "home"
        for d in (".ssh", ".claude/plugins", ".claude/projects/other", ".codex", ".config/gitea", "work"):
            (self.home / d).mkdir(parents=True)
        (self.home / ".ssh" / "known_hosts").write_text("github.com ssh-ed25519 AAAA\n")
        (self.home / ".ssh" / "id_ed25519").write_text("not a real key\n")
        (self.home / ".claude" / "settings.json").write_text("{}")
        (self.home / ".claude" / ".credentials.json").write_text("{}")
        (self.home / ".claude.json").write_text('{"x": 1}')
        (self.home / ".codex" / "auth.json").write_text("{}")
        self.priv = self.home / ".local/share/swarm/supervisor"
        self.priv.mkdir(parents=True, mode=0o700)
        os.chmod(self.priv, 0o700)
        self.work = self.home / "work"
        self.cfg = {"drive": {"seat_git_name": "Seat", "seat_git_email": "seat@example.test"},
                    "supervise": {"allowed_workdirs": [str(self.work)]}}
        for p in (mock.patch.dict(os.environ, {"HOME": str(self.home)}),
                  mock.patch("swarm.supervisor.seathome.pinned_paths", lambda cfg: [])):
            p.start()
            self.addCleanup(p.stop)

    def git(self, *args, cwd=None):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], check=True, cwd=cwd,
                       capture_output=True)

    def worktree(self) -> str:
        self.git("init", "-q", str(self.work / "repo"))
        self.git("-C", str(self.work / "repo"), "commit", "-q", "--allow-empty", "-m", "x")
        self.git("-C", str(self.work / "repo"), "worktree", "add", "-q", str(self.work / "wt"), "-b", "seat")
        return str(self.work / "wt")


class PrepareTest(SeatHomeCase):
    def test_the_seat_directory(self):
        h = seathome.prepare(self.cfg, "J", "eng-1")
        self.assertTrue(str(h).startswith(str(self.priv)))
        self.assertEqual(os.stat(h).st_mode & 0o777, 0o700)
        self.assertEqual(sorted(os.listdir(h / ".ssh")), ["config", "known_hosts"])
        self.assertIn("IdentityAgent none", (h / ".ssh" / "config").read_text())
        git = (h / "gitconfig").read_text()
        self.assertIn('name = "Seat"', git)
        self.assertIn("[credential]\n\thelper =\n", git)
        self.assertEqual((h / "home" / ".claude.json").read_text(), '{"x": 1}')
        self.assertTrue(os.access(h / "bin" / "swarm", os.X_OK))
        self.assertTrue((h / "bin" / "seatbroker_client.py").is_file())

    def test_a_planted_link_is_replaced_not_followed(self):
        h = seathome.prepare(self.cfg, "J", "eng-1")
        victim = self.tmp / "victim"
        victim.write_text("keep")
        (h / "home" / ".claude.json").unlink()
        (h / "home" / ".claude.json").symlink_to(victim)
        seathome.prepare(self.cfg, "J", "eng-1")
        self.assertEqual(victim.read_text(), "keep")

    def test_a_relative_seat_key_is_refused(self):
        self.cfg["drive"]["seat_ssh_key"] = "keys/ro"
        with self.assertRaises(seathome.SeatIsolationError):
            seathome.prepare(self.cfg, "J", "eng-1")


class SpecTest(SeatHomeCase):
    def spec(self, workdir=None):
        h = seathome.prepare(self.cfg, "J", "s")
        return seathome.jail_spec(self.cfg, h, workdir, self.home), h

    def test_an_allowlist(self):
        wt = self.worktree()
        spec, h = self.spec(wt)
        binds = {b[1]: b[2] for b in spec["binds"]}
        H = str(self.home)
        self.assertEqual(spec["home_base"], f"{h}/home")
        self.assertEqual(spec["tmp"], f"{h}/tmp")
        self.assertEqual(binds[f"{H}/.claude"], "rw")                    # the seat's own ~/.claude
        self.assertEqual(binds[f"{H}/.claude/settings.json"], "ro")
        self.assertEqual(binds[f"{H}/.claude/plugins"], "ro")
        self.assertEqual(binds[f"{H}/.claude/.credentials.json"], "rw")
        self.assertEqual(binds[f"{H}/.codex/auth.json"], "rw")
        self.assertEqual(binds[f"{H}/.ssh"], "ro")
        self.assertEqual(binds[wt], "rw")
        common = str(self.work / "repo" / ".git")
        self.assertEqual(binds[common], "rw")
        self.assertEqual(binds[f"{common}/hooks"], "ro")
        self.assertEqual(binds[f"{common}/config"], "ro")
        for hidden in (".claude/projects", ".config", ".config/gitea", ".local/share/swarm/supervisor"):
            self.assertNotIn(f"{H}/{hidden}", binds)
        self.assertEqual(spec["cwd"], wt)

    def test_a_gitdir_line_pointing_elsewhere_is_refused(self):
        wt = self.work / "evil"
        wt.mkdir()
        (wt / ".git").write_text(f"gitdir: {self.home}/.ssh\n")
        with self.assertRaises(seathome.SeatIsolationError):
            self.spec(str(wt))

    def test_pins_outside_the_home_are_covered(self):
        with mock.patch("swarm.supervisor.seathome.pinned_paths", lambda cfg: ["/srv/keys/id", f"{self.home}/.ssh/x"]):
            spec, _ = self.spec()
        self.assertEqual(spec["cover"], ["/srv/keys/id"])   # inside the home it is simply not bound

    def test_pin_paths(self):
        owner = {"credentials": {"a": "ssh-key:/k/id", "b": "ssh-agent:/run/a.sock", "c": "helper:/usr/bin/gh x"},
                 "config_dirs": {"c": "/cfg/gh"}}
        self.assertEqual(sorted(seathome._pin_paths(owner)), ["/cfg/gh", "/k/id", "/k/id.pub", "/run/a.sock"])

    def test_platform_lists(self):
        self.assertIn("Library/Application Support/gh", seathome.platform_cover("darwin"))
        self.assertIn("AppData/Roaming/GitHub CLI", seathome.platform_cover("win32"))


class FailClosedTest(SeatHomeCase):
    def test_unreadable_pins_refuse(self):
        mock.patch.stopall()
        with mock.patch.dict(os.environ, {"HOME": str(self.home)}), \
                mock.patch("swarm.supervisor.ledger.unfinished", side_effect=OSError("boom")):
            with self.assertRaises(seathome.SeatIsolationError):
                seathome.pinned_paths(self.cfg)

    def test_config_cannot_turn_isolation_off(self):
        self.cfg["drive"]["seat_isolation"] = "off"
        self.assertEqual(seathome.mode(self.cfg), "required")

    def test_turning_it_off_is_a_person_act(self):
        with self.assertRaises(person.PersonRefused):
            seathome.set_off(True, env={"CLAUDE_CODE_SESSION_ID": "s"}, ancestor=lambda: None)
        self.assertEqual(seathome.mode(self.cfg), "required")
        with mock.patch("swarm.enrolment.agent_sessions", lambda bk: set()):
            seathome.set_off(True, env={}, confirm=lambda a, j: True, ancestor=lambda: None)
        self.assertEqual(seathome.mode(self.cfg), "off")
        self.assertEqual(seathome.wrap(self.cfg, ["claude"], job="J", seat="s", probe=lambda: (False, "x")),
                         ["claude"])
        with mock.patch("swarm.enrolment.agent_sessions", lambda bk: set()):
            seathome.set_off(False, env={}, confirm=lambda a, j: True, ancestor=lambda: None)
        self.assertEqual(seathome.mode(self.cfg), "required")

    def test_a_host_that_cannot_jail_refuses_the_seat(self):
        with self.assertRaises(seathome.SeatIsolationError) as cm:
            seathome.wrap(self.cfg, ["claude"], job="J", seat="s", probe=lambda: (False, "no userns"))
        self.assertIn("no userns", str(cm.exception))

    def test_runner_wraps_drive_seats_only(self):
        wt = self.worktree()
        run = {"argv": ["claude", "-p"], "job": "J", "name": "Lisa", "seat": "eng", "cwd": wt}
        with mock.patch("swarm.seatjail.available", lambda: (True, "")):
            self.assertEqual(runner._seat_argv(self.cfg, dict(run)), (["claude", "-p"], None))
            drive = {**run, "launch_id": "L1"}
            argv, why = runner._seat_argv(self.cfg, drive)
        self.assertIsNone(why)
        self.assertEqual(argv[-3:], ["--", "claude", "-p"])
        self.assertTrue(drive["seat_home"].startswith(str(self.priv)))
        with mock.patch("swarm.seatjail.available", lambda: (False, "no userns")):
            _, why = runner._seat_argv(self.cfg, {**run, "launch_id": "L1"})
        self.assertIn("no userns", why)

    def test_no_namespaces_means_nothing_runs(self):
        with mock.patch.object(seatjail, "_unshare", side_effect=OSError(1, "unshare: EPERM")), \
                mock.patch("os.execvpe") as ex, mock.patch("os.fork") as fork:
            self.assertEqual(seatjail.run({}, ["true"]), seatjail.EXIT_UNAVAILABLE)
        ex.assert_not_called()
        fork.assert_not_called()


class BrokerUnitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tmpdir(self)
        self.wd = self.tmp / "wt"
        self.wd.mkdir()
        (self.wd / "report.md").write_text("ok")
        self.seat = self.tmp / "seat"
        (self.seat / "broker").mkdir(parents=True)
        self.outside = self.tmp / "secret"
        self.outside.write_text("x")
        self.calls = []

        def fake_run(argv, **kw):
            self.calls.append((argv, kw))
            return subprocess.CompletedProcess(argv, 0, b"ran\n", b"")
        self.run = {"harness": "claude", "session_id": "sid-1", "name": "Lisa"}
        self.b = seatbroker.SeatBroker(self.run, str(self.seat / "broker"), workdir=str(self.wd),
                                       roots=[str(self.wd), str(self.seat)], jail_home="/home/u",
                                       seat_dir=str(self.seat), host_pid=4242, bin_dir="/plugin/bin",
                                       base_env={"PATH": "/bin", "CLAUDE_CODE_SESSION_ID": "other",
                                                 "SWARM_CONFIG": "/c"}, runner=fake_run)

    def ask(self, kind, argv, stdin=None, cwd=None):
        d = os.open(str(self.seat / "broker"), os.O_RDONLY)
        try:
            out = {}

            def go():
                out["r"] = seatbroker_client.request(kind, argv, str(self.seat / "broker"), stdin, timeout=10)
            with mock.patch("os.getcwd", lambda: cwd or str(self.wd)):
                t = threading.Thread(target=go)
                t.start()
                for _ in range(200):
                    if self.b.poll_once(d, inline=True):
                        break
                    threading.Event().wait(0.02)
                t.join(10)
            return out["r"]
        finally:
            os.close(d)

    def test_a_post_runs_outside_as_the_seat(self):
        rc, out, _ = self.ask("cli", ["post", "--job", "J", "--as", "Lisa", "hello"])
        self.assertEqual((rc, out), (0, b"ran\n"))
        argv, kw = self.calls[-1]
        self.assertEqual(argv, ["/plugin/bin/swarm", "post", "--job", "J", "--as", "Lisa", "hello"])
        self.assertEqual(kw["env"]["CLAUDE_CODE_SESSION_ID"], "sid-1")
        self.assertEqual(kw["env"]["SWARM_HOST"], "claude")
        self.assertEqual(kw["env"][person.BROKER_PID_ENV], "4242")
        self.assertEqual(kw["cwd"], str(self.wd))

    def test_refusals(self):
        for argv in (["bg", "--", "id"], ["config", "set", "x", "y"], ["supervise", "seat-isolation", "off"],
                     ["--config", "/x", "post"], ["transcript", "show"], ["activate", "--job", "J"],
                     ["post", "--job", "J", "--as", "Bart", "forged"], ["post", "--job", "J", "--key=other", "x"],
                     ["verdict", "--job", "J", "met", "--details", str(self.outside)],
                     ["verdict", "--job", "J", "met", f"--details={self.outside}"]):
            rc, _, err = self.ask("cli", argv)
            self.assertEqual(rc, 2, argv)
            self.assertIn(b"refused in a drive seat", err)
        self.assertEqual(self.calls, [])

    def test_a_file_in_the_work_tree_may_be_named(self):
        rc, _, _ = self.ask("cli", ["verdict", "--job", "J", "met", "--details", "report.md"])
        self.assertEqual(rc, 0)

    def test_a_hook_payload_is_stamped(self):
        payload = {"session_id": "forged", "transcript_path": "/home/u/.claude/projects/p/x.jsonl",
                   "agent_transcript_path": "/home/u/.ssh/id", "cwd": "/home/u", "tool_name": "Bash"}
        rc, _, _ = self.ask("hook", ["--host", "claude", "turn"], json.dumps(payload).encode())
        self.assertEqual(rc, 0)
        argv, kw = self.calls[-1]
        self.assertEqual(argv, ["/plugin/bin/swarm-hook", "--host", "claude", "turn"])
        sent = json.loads(kw["input"])
        self.assertEqual(sent["session_id"], "sid-1")
        self.assertEqual(sent["transcript_path"], f"{self.seat}/claude/projects/p/x.jsonl")
        self.assertNotIn("agent_transcript_path", sent)
        self.assertEqual(sent["cwd"], str(self.wd))

    def test_a_hook_of_another_harness_is_refused(self):
        rc, _, _ = self.ask("hook", ["--host", "codex", "turn"], b"{}")
        self.assertEqual(rc, 2)

    def test_a_planted_link_request_is_not_read(self):
        (self.seat / "broker" / f"{'a' * 32}.req").symlink_to(self.outside)
        d = os.open(str(self.seat / "broker"), os.O_RDONLY)
        try:
            self.b.poll_once(d, inline=True)
        finally:
            os.close(d)
        resp = json.loads((self.seat / "broker" / f"{'a' * 32}.resp").read_text())
        self.assertEqual(resp["rc"], 2)
        self.assertEqual(self.calls, [])


class DoctorTest(unittest.TestCase):
    def test_lines(self):
        self.assertTrue(bootstrap._seat_isolation_check({}, probe=lambda c: (True, "active")).ok)
        leak = bootstrap._seat_isolation_check({}, probe=lambda c: (False, "KEY /h/.ssh/id_ed25519"))
        self.assertFalse(leak.ok)
        self.assertIsNone(bootstrap._seat_isolation_check({}, probe=lambda c: (None, "off (a person ...)")).ok)
        self.assertFalse(bootstrap._seat_isolation_check({}, probe=lambda c: (None, "unavailable: EPERM")).ok)

    def test_the_probe_reports_what_it_reaches(self):
        tmp = tmpdir(self)
        (tmp / ".ssh").mkdir()
        (tmp / ".ssh" / "id_ed25519").write_text("x")
        (tmp / "gh").mkdir()
        (tmp / "gh" / "hosts.yml").write_text("x")
        env = {"PATH": os.environ["PATH"], "HOME": str(tmp), "PGPASSWORD": "x", "GIT_CONFIG_NOSYSTEM": "1",
               "GIT_CONFIG_GLOBAL": os.devnull, "GH_CONFIG_DIR": str(tmp / "nogh")}
        r = subprocess.run(["sh", "-c", seathome.PROBE, "probe", str(tmp / "gh")], capture_output=True,
                           text=True, env=env, timeout=60)
        self.assertEqual(r.returncode, 1)
        for word in ("LISTABLE", "KEY", "ENV"):
            self.assertIn(word, r.stdout)


SEAT_PROBE = textwrap.dedent("""\
    import ctypes, json, os, socket, subprocess, sys
    home, outside = sys.argv[1], sys.argv[2]
    out = {"uid": os.getuid(), "pid": os.getpid(), "cwd": os.getcwd(),
           "env": sorted(k for k in os.environ if k in ("GH_TOKEN", "PGPASSWORD", "TMUX", "HERDR_SOCKET_PATH")),
           "jail": os.environ.get("SWARM_SEAT_JAIL")}
    out["planted"] = os.path.exists(os.path.join(home, ".config/newtool/token"))
    out["ssh_key"] = os.path.exists(os.path.join(home, ".ssh/id_ed25519"))
    out["projects"] = os.path.exists(os.path.join(home, ".claude/projects/other"))
    out["settings_w"] = os.access(os.path.join(home, ".claude/settings.json"), os.W_OK)
    out["settings_r"] = os.access(os.path.join(home, ".claude/settings.json"), os.R_OK)
    out["hooks_w"] = os.access(os.path.join(home, "work/repo/.git/hooks"), os.W_OK)
    out["objects_w"] = os.access(os.path.join(home, "work/repo/.git/objects"), os.W_OK)
    out["wt_w"] = os.access(os.getcwd(), os.W_OK)
    out["outside"] = os.path.exists(outside)
    def connect(p):
        s = socket.socket(socket.AF_UNIX)
        try:
            s.connect(p); return True
        except OSError:
            return False
        finally:
            s.close()
    out["herdr"] = connect(os.path.join(home, ".config/herdr/sessions/s/herdr.sock"))
    out["tmux"] = connect(sys.argv[3])
    libc = ctypes.CDLL(None, use_errno=True)
    out["umount"] = libc.umount2(os.path.join(home, ".claude/settings.json").encode(), 2)
    out["remount"] = libc.mount(None, os.path.join(home, ".claude/settings.json").encode(), None,
                                ctypes.c_ulong(32 | 4096), None)
    up = os.path.join(os.getcwd(), "..", "..")
    out["dotdot"] = sorted(os.listdir(up)) + sorted(os.listdir(os.path.join(up, ".ssh")))
    print(json.dumps(out))
    """)


@unittest.skipUnless(CAN_JAIL, "needs unprivileged user namespaces (Linux)")
class JailTest(SeatHomeCase):
    """The real jail through seathome's own spec, on a fake person home."""

    def test_the_seat_sees_only_its_allowlist(self):
        wt = self.worktree()
        (self.home / ".claude" / "projects" / "other" / "t.jsonl").write_text("someone else's transcript")
        (self.home / ".config" / "newtool").mkdir(parents=True)
        (self.home / ".config" / "newtool" / "token").write_text("an unknown secret")
        sock_dir = self.home / ".config" / "herdr" / "sessions" / "s"
        sock_dir.mkdir(parents=True)
        herdr = socket.socket(socket.AF_UNIX)
        herdr.bind(str(sock_dir / "herdr.sock"))
        herdr.listen(1)
        self.addCleanup(herdr.close)
        tmux_dir = Path(tempfile.mkdtemp(prefix="tmux-test-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, tmux_dir, True)
        tmux = socket.socket(socket.AF_UNIX)
        tmux.bind(str(tmux_dir / "default"))
        tmux.listen(1)
        self.addCleanup(tmux.close)
        (Path(wt) / "probe.py").write_text(SEAT_PROBE)
        with mock.patch("swarm.seatjail.available", lambda: (True, "")):
            argv = seathome.wrap(self.cfg, [sys.executable, "-I", "probe.py", str(self.home), str(self.tmp / "x"),
                                            str(tmux_dir / "default")], job="J", seat="s", workdir=wt)
        (self.tmp / "x").write_text("outside the home")
        env = {**os.environ, "GH_TOKEN": "x", "PGPASSWORD": "y", "TMUX": "z", "HERDR_SOCKET_PATH": "w"}
        r = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out["uid"], os.getuid())
        self.assertEqual(out["pid"], 2)
        self.assertEqual(out["cwd"], wt)
        self.assertEqual(out["env"], [])
        self.assertEqual(out["jail"], "1")
        self.assertFalse(out["planted"])            # an unknown secret is not there (allowlist)
        self.assertFalse(out["ssh_key"])
        self.assertFalse(out["projects"])           # other sessions' transcripts
        self.assertTrue(out["settings_r"])
        self.assertFalse(out["settings_w"])         # harness settings read-only
        self.assertFalse(out["hooks_w"])            # git hooks read-only
        self.assertTrue(out["objects_w"])
        self.assertTrue(out["wt_w"])
        self.assertFalse(out["herdr"])              # no control socket of the person's
        self.assertFalse(out["tmux"])
        self.assertEqual(out["umount"], -1)
        self.assertEqual(out["remount"], -1)        # a read-only bind cannot be made writable
        self.assertNotIn("id_ed25519", out["dotdot"])   # .. from the work tree is the seat's home, not the person's
        self.assertNotIn(".config", out["dotdot"])
        self.assertEqual((self.home / ".ssh" / "id_ed25519").read_text(), "not a real key\n")


FAKE_SEAT = textwrap.dedent("""\
    import json, subprocess, sys
    job, name = sys.argv[1], sys.argv[2]
    def sw(*a):
        r = subprocess.run(["swarm", *a], capture_output=True, text=True)
        return {"rc": r.returncode, "out": r.stdout.strip()[-300:], "err": r.stderr.strip()[-300:]}
    print(json.dumps({
        "post": sw("post", "--job", job, "--as", name, "hello from the jail"),
        "forged": sw("post", "--job", job, "--as", "Somebody Else", "forged"),
        "read": sw("read", "--job", job, "--as", name),
        "verdict": sw("verdict", "--job", job, "--as", name, "met", "all requirements hold"),
        "bg": sw("bg", "--", "id"),
    }))
    """)


@unittest.skipUnless(CAN_JAIL, "needs unprivileged user namespaces (Linux)")
class BrokerEndToEndTest(SeatHomeCase):
    """A fake-harness seat in the real jail, with no board access of its own, posts, reads and records a
    verdict through its broker; the broker stamps who it is."""

    def test_post_read_verdict(self):
        board_dir = self.tmp / "board"
        board_dir.mkdir()
        config = board_dir / "config.toml"
        config.write_text(f'[board]\nbackend = "sqlite"\nspool_dir = "{board_dir}/spool"\n'
                          f'[sqlite]\npath = "{board_dir}/board.sqlite3"\n[hook]\nmarker_dir = "{board_dir}/m"\n')
        bindir = self.tmp / "bin"
        bindir.mkdir()
        (bindir / "swarm").write_text(f'#!/bin/sh\nexec "{sys.executable}" -m swarm.cli "$@"\n')
        os.chmod(bindir / "swarm", 0o755)
        env = {**os.environ, "SWARM_CONFIG": str(config), "PYTHONPATH": str(LIB), "SWARM_AUTO_INIT": "1"}
        for v in ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "CODEX_SESSION_ID", "CLAUDECODE"):
            env.pop(v, None)

        def sw(*a, **kw):
            r = subprocess.run([str(bindir / "swarm"), *a], capture_output=True, text=True,
                               env={**env, **kw}, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            return r.stdout.strip().splitlines()[-1]
        sw("activate", "--job", "T20", "--session", "main-1")
        other = sw("join", "--job", "T20", "--key", "other-key", "--role", "worker")
        sid = str(uuid.uuid4())
        name = sw("join", "--job", "T20", "--key", sid, "--role", "judge", "--judge")
        wt = self.worktree()
        (Path(wt) / "seat.py").write_text(FAKE_SEAT)
        with mock.patch("swarm.seatjail.available", lambda: (True, "")):
            argv = seathome.wrap(self.cfg, [sys.executable, "-I", "seat.py", "T20", name], job="T20", seat="judge",
                                 workdir=wt)
        sh = str(seathome.home_dir("T20", "judge"))
        run = {"harness": "claude", "session_id": sid, "name": name, "cwd": wt}
        broker = seatbroker.SeatBroker(run, f"{sh}/broker", workdir=wt, roots=[wt, sh], jail_home=str(self.home),
                                       seat_dir=sh, host_pid=None, bin_dir=str(bindir), base_env=env).start()
        try:
            r = subprocess.run(argv, capture_output=True, text=True, env={**env, "PGPASSWORD": "never"},
                               timeout=120)
        finally:
            broker.stop()
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertEqual(out["post"]["rc"], 0, out)
        self.assertEqual(out["forged"]["rc"], 2, out)
        self.assertEqual(out["read"]["rc"], 0, out)
        self.assertEqual(out["verdict"]["rc"], 0, out)
        self.assertIn("recorded", out["verdict"]["out"])
        self.assertEqual(out["bg"]["rc"], 2, out)
        import sqlite3
        with sqlite3.connect(board_dir / "board.sqlite3") as c:
            rows = c.execute("select * from messages").fetchall()
        self.assertTrue(any(name in row and "hello from the jail" in row for row in rows), rows)
        self.assertFalse(any("forged" in row for row in rows))


class ClientTest(unittest.TestCase):
    def test_no_broker_dir(self):
        with mock.patch.dict(os.environ, {"SWARM_SEAT_BROKER": "/nonexistent"}):
            self.assertEqual(seatbroker_client.main(["hook", "--host", "claude", "turn"]), 0)
            self.assertEqual(seatbroker_client.main(["cli", "post"]), 1)

    def test_a_dead_broker_times_out(self):
        d = tmpdir(self)
        clock = iter(range(0, 1000, 10))
        rc, _, err = seatbroker_client.request("cli", ["read"], str(d), timeout=5, clock=lambda: next(clock),
                                               sleep=lambda s: None)
        self.assertEqual(rc, 124)
        self.assertIn(b"did not answer", err)

    def test_answer_roundtrip(self):
        d = tmpdir(self)
        resp = {"rc": 3, "out": base64.b64encode(b"o").decode(), "err": base64.b64encode(b"e").decode()}

        def sleep(_):
            for n in os.listdir(d):
                if n.endswith(".req"):
                    (d / (n[:-4] + ".resp")).write_text(json.dumps(resp))
        self.assertEqual(seatbroker_client.request("cli", ["read"], str(d), timeout=5, sleep=sleep), (3, b"o", b"e"))


if __name__ == "__main__":
    unittest.main()
