"""Task 20: drive seats run in a jail with an allowlisted file view and no board access of their own
(swarm.seatjail, supervisor.seathome, supervisor.seatbroker, swarm.seatbroker_client)."""
from __future__ import annotations

import base64
import contextlib
import io
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
        with mock.patch("swarm.seatjail.available", lambda: (False, "no userns")), \
                mock.patch("swarm.seatjail.no_userns_reason", lambda: "unprivileged user namespaces are disabled"):
            fallback = {**run, "launch_id": "L1"}
            argv, why = runner._seat_argv(self.cfg, fallback)       # no jail here: best-effort, never refused
            self.assertIsNone(why)
            self.assertEqual((argv, fallback["seat_mode"]), (["claude", "-p"], "best-effort"))
            self.cfg["drive"]["seat_isolation"] = "strict"
            _, why = runner._seat_argv(self.cfg, {**run, "launch_id": "L1"})
        self.assertIn("no userns", why)                               # strict: refused

    def test_no_namespaces_means_nothing_runs(self):
        with mock.patch.object(seatjail, "_unshare", side_effect=OSError(1, "unshare: EPERM")), \
                mock.patch("os.execvpe") as ex, mock.patch("os.fork") as fork:
            self.assertEqual(seatjail.run({}, ["true"]), seatjail.EXIT_UNAVAILABLE)
        ex.assert_not_called()
        fork.assert_not_called()


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


def recording_runner(calls, rc=0, out=b"ran\n", gate=None, attested=None):
    def run(argv, **kw):
        calls.append((argv, kw))
        if attested is not None:
            attested.append(person.broker_attested(kw["env"], attest_dir=attested_dir(kw)))
        if gate is not None:
            gate.wait(10)
        return subprocess.CompletedProcess(argv, rc, out, b"")
    return run


def cli_argv(call) -> list[str]:
    """The swarm argv of a brokered call: what follows the pinned --config."""
    argv = call[0]
    i = argv.index("--config")
    return argv[i + 2:]


_ATTEST_DIRS: dict = {}


def attested_dir(kw):
    return _ATTEST_DIRS.get("dir")


class BrokerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tmpdir(self)
        self.wd = self.tmp / "wt"
        self.wd.mkdir()
        self.seat = self.tmp / "seat"
        (self.seat / "broker").mkdir(parents=True)
        self.files = self.tmp / "private-files"
        self.files.mkdir()
        self.calls = []
        self.attested = []
        self.run = {"harness": "claude", "session_id": "11111111-2222-3333-4444-555555555555", "name": "Lisa"}
        _ATTEST_DIRS["dir"] = str(self.tmp / "attest")
        self.b = self.broker()

    def broker(self, runner=None, **kw):
        kw.setdefault("attest_dir", str(self.tmp / "attest"))
        return seatbroker.SeatBroker(self.run, str(self.seat / "broker"), workdir=str(self.wd), forbid=[],
                                     seat_dir=str(self.seat), config="/sup/config.toml", host_pid=4242,
                                     base_env={"PATH": "/bin", "CLAUDE_CODE_SESSION_ID": "other",
                                               "PYTHONPATH": str(self.wd)},
                                     runner=runner or recording_runner(self.calls, attested=self.attested),
                                     file_dir=str(self.files), **kw)

    def cli(self, argv, broker=None):
        """The seat side (seatbroker_client.run_cli, cwd the work tree) against this broker, inline."""
        b = broker or self.b
        d = os.open(str(self.seat / "broker"), os.O_RDONLY)
        out = {}
        old = os.getcwd()
        os.chdir(self.wd)
        try:
            def go():
                out["r"] = seatbroker_client.run_cli(argv, str(self.seat / "broker"), timeout=10)
            t = threading.Thread(target=go)
            t.start()
            for _ in range(300):
                if b.poll_once(d, inline=True) or not t.is_alive():
                    break
                threading.Event().wait(0.02)
            t.join(10)
            return out["r"]
        finally:
            os.chdir(old)
            os.close(d)

    def raw(self, body: dict):
        """A request written straight into the broker dir (a seat need not use the client)."""
        rid = uuid.uuid4().hex
        (self.seat / "broker" / f"{rid}.req").write_text(json.dumps(body))
        d = os.open(str(self.seat / "broker"), os.O_RDONLY)
        try:
            self.b.poll_once(d, inline=True)
        finally:
            os.close(d)
        r = json.loads((self.seat / "broker" / f"{rid}.resp").read_text())
        return r["rc"], base64.b64decode(r["out"]), base64.b64decode(r["err"])


class TypedProtocolTest(BrokerCase):
    def test_a_done_without_job_runs_as_the_seats_own_job(self):
        # QA live-met fdd441e: "refused in a drive seat: missing job" on the seat's first `swarm done`
        self.run["job"] = "J"
        rc, out, err = self.cli(["done", "--artifact", "feat/a@" + "a" * 40, "--summary", "x"])
        self.assertEqual((rc, out), (0, b"ran\n"), err)
        self.assertIn("--job=J", cli_argv(self.calls[-1]))

    def test_a_done_naming_another_job_is_refused(self):
        self.run["job"] = "J"
        n = len(self.calls)
        rc, _, err = self.cli(["done", "--job", "K", "--artifact", "feat/a@" + "a" * 40])
        self.assertEqual(rc, 2)
        self.assertIn(b"not this seat's job", err)
        self.assertEqual(len(self.calls), n)

    def test_a_post_is_built_by_the_broker(self):
        rc, out, _ = self.cli(["post", "--job", "J", "--as", "Bart", "hello", "world"])
        self.assertEqual((rc, out), (0, b"ran\n"))
        argv, kw = self.calls[-1]
        self.assertEqual(argv[1:3], ["-I", "-c"])
        self.assertEqual(argv[argv.index("--config") + 1], "/sup/config.toml")
        self.assertEqual(cli_argv(self.calls[-1]),
                         ["post", "--job=J", f"--key={self.run['session_id']}", "--", "hello", "world"])
        self.assertEqual(kw["cwd"], "/")
        self.assertEqual(kw["env"]["CLAUDE_CODE_SESSION_ID"], self.run["session_id"])
        self.assertEqual(kw["env"]["SWARM_CONFIG"], "/sup/config.toml")
        self.assertNotIn("PYTHONPATH", kw["env"])
        self.assertIn(person.BROKER_TOKEN_ENV, kw["env"])
        self.assertNotIn("SWARM_BROKER_HOST_PID", kw["env"])
        self.assertEqual(self.attested, [4242])                    # the record the broker wrote for the call
        self.assertEqual(os.listdir(self.tmp / "attest"), [])     # and removed after it

    def test_rr9_no_config_or_plugin_reaches_the_cli(self):
        (self.wd / "evil").mkdir()
        (self.wd / "evil" / "none.toml").write_text("")
        for argv in (["post", "--job", "J", "--config", "evil/none.toml", "x"],
                     ["post", "--job", "J", "--conf=evil/none.toml", "x"],
                     ["post", "--config=evil/none.toml", "--job", "J", "x"],
                     ["--config", "evil/none.toml", "post", "--job", "J", "x"]):
            rc, _, err = self.cli(argv)
            self.assertEqual(rc, 2, argv)
        rc, _, _ = self.raw({"v": 2, "kind": "cli", "command": "post",
                             "fields": {"job": "J", "message": ["x"], "config": "evil/none.toml"}})
        self.assertEqual(rc, 2)
        self.assertEqual(self.calls, [])

    def test_rr9_the_cli_honours_only_a_leading_config_for_plugins(self):
        from swarm import cli
        seen = []
        with mock.patch("swarm.plugins.Registry") as reg:
            reg.side_effect = lambda cfg, path, **kw: seen.append(str(path)) or mock.MagicMock()
            cli._load_plugins(["post", "--config", "/evil/none.toml", "--job", "J", "x"], cli._parser())
            cli._load_plugins(["post", "--conf=/evil/none.toml"], cli._parser())
            cli._load_plugins(["--config", "/good/c.toml", "post"], cli._parser())
        self.assertNotIn("/evil/none.toml", seen)
        self.assertEqual(seen[-1], "/good/c.toml")

    def test_rr10_abbreviated_and_explicit_identity_is_never_passed(self):
        for argv in (["post", "--job", "J", "--a", "Bart", "x"], ["post", "--job", "J", "--a=Bart", "x"],
                     ["land", "--job", "J", "--ke", "OTHER"], ["review", "--job", "J", "verified", "--k", "OTHER"],
                     ["done", "--job", "J", "--ke", "OTHER"], ["read", "--job", "J", "--sess", "x"]):
            rc, _, _ = self.cli(argv)
            self.assertEqual(rc, 2, argv)
        self.assertEqual(self.calls, [])
        rc, _, _ = self.cli(["done", "--job", "J", "--key", "OTHER"])   # accepted and DROPPED
        self.assertEqual(rc, 0)
        self.assertEqual(cli_argv(self.calls[-1]), ["done", "--job=J", f"--key={self.run['session_id']}"])
        for field in ("as", "key", "session", "name", "_as", "agent_key"):
            rc, _, err = self.raw({"v": 2, "kind": "cli", "command": "done", "fields": {"job": "J", field: "x"}})
            self.assertEqual(rc, 2, field)
        rc, _, _ = self.cli(["verdict", "--job", "J", "--as", "Bart", "met", "ok"])
        self.assertEqual(cli_argv(self.calls[-1]), ["verdict", "--job=J", "--as=Lisa", "--", "met", "ok"])

    def test_rr11_a_file_field_is_content_never_a_seat_path(self):
        (self.wd / "r.md").write_text("the report")
        seen = []

        def runner(argv, **kw):
            p = next(a for a in cli_argv((argv,)) if a.startswith("--details="))[len("--details="):]
            seen.append((p, Path(p).read_text()))
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        b = self.broker(runner)
        rc, _, _ = self.cli(["verdict", "--job", "J", "met", "ok", "--details", "r.md"], broker=b)
        self.assertEqual(rc, 0)
        path, content = seen[0]
        self.assertTrue(path.startswith(str(self.files)))
        self.assertEqual(content, "the report")
        self.assertFalse(Path(path).exists())       # removed after the call
        rc, _, _ = self.raw({"v": 2, "kind": "cli", "command": "verdict",
                             "fields": {"job": "J", "verdict": "met", "details": "/home/u/.secrets/x"}})
        argv = cli_argv(self.calls[-1]) if self.calls else []
        # the content "/home/u/.secrets/x" is what lands in the file: the broker never opens that path
        self.assertTrue(all("/home/u" not in a for a in argv))

    def test_rr12_a_codex_seat_waits_for_its_thread(self):
        self.run.clear()
        self.run.update({"harness": "codex", "session_id": None, "name": "Bart"})
        rc, _, err = self.cli(["post", "--job", "J", "x"])
        self.assertEqual(rc, 2)
        self.assertIn(b"not known yet", err)
        rc, out, _ = self.raw({"v": 2, "kind": "hook", "event": "turn", "payload": {"session_id": "forged"}})
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(self.calls, [])
        self.run["agent_key"] = "019a0000-0000-7000-8000-000000000001"
        self.raw({"v": 2, "kind": "hook", "event": "turn", "payload": {"session_id": "forged", "tool_name": "Bash"}})
        argv, kw = self.calls[-1]
        self.assertEqual(json.loads(kw["input"])["session_id"], self.run["agent_key"])
        self.assertEqual(kw["env"]["CODEX_THREAD_ID"], self.run["agent_key"])

    def test_hook_payload_is_whitelisted_and_stamped(self):
        proj = self.seat / "claude" / "projects" / "p"
        proj.mkdir(parents=True)
        (proj / f"{self.run['session_id']}.jsonl").write_text("")
        self.raw({"v": 2, "kind": "hook", "event": "turn",
                  "payload": {"session_id": "forged", "transcript_path": "/home/u/.ssh/id", "cwd": "/home/u",
                              "tool_name": "Bash", "tool_input": {"command": "ls"}, "evil": "x"}})
        argv, kw = self.calls[-1]
        self.assertEqual(cli_argv(self.calls[-1]), ["hook", "--host", "claude", "turn"])
        sent = json.loads(kw["input"])
        self.assertEqual(sent, {"tool_name": "Bash", "tool_input": {"command": "ls"},
                                "session_id": self.run["session_id"],
                                "transcript_path": str(proj / f"{self.run['session_id']}.jsonl"),
                                "cwd": str(self.wd)})
        rc, _, _ = self.raw({"v": 2, "kind": "hook", "event": "../bin", "payload": {}})
        self.assertEqual(rc, 2)

    def test_rr13_hooks_have_their_own_lane_and_fail_closed(self):
        gate = threading.Event()
        self.addCleanup(gate.set)
        b = self.broker(recording_runner(self.calls, gate=gate)).start()
        self.addCleanup(b.stop)
        bdir = str(self.seat / "broker")
        for _ in range(seatbroker.CLI_WORKERS + 1):     # every cli worker blocked on a long call
            threading.Thread(target=seatbroker_client.request,
                             args=({"v": 2, "kind": "cli", "command": "wait",
                                    "fields": {"job": "J", "for": "15m"}}, bdir, 5), daemon=True).start()
        threading.Event().wait(0.5)
        gate_hooks = threading.Event()
        b._run = recording_runner(self.calls, out=b'{"ok": 1}')   # hooks answer at once
        r = seatbroker_client.request({"v": 2, "kind": "hook", "event": "turn", "payload": {}}, bdir, 5)
        self.assertIsNotNone(r)
        self.assertEqual(r[1], b'{"ok": 1}')
        gate_hooks.set()
        # no broker at all, or no answer: the guard denies
        rc, out, _ = seatbroker_client.run_hook(["--host", "claude", "turn"], str(self.tmp / "missing"), b"{}")
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["permissionDecision"], "deny")
        empty = tmpdir(self)
        rc, out, _ = seatbroker_client.run_hook(["--host", "claude", "turn"], str(empty), b"{}", timeout=0.2)
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_rr13_a_failing_guard_hook_denies(self):
        b = self.broker(recording_runner(self.calls, rc=1, out=b""))
        d = os.open(str(self.seat / "broker"), os.O_RDONLY)
        try:
            rid = uuid.uuid4().hex
            (self.seat / "broker" / f"{rid}.req").write_text(
                json.dumps({"v": 2, "kind": "hook", "event": "turn", "payload": {}}))
            b.poll_once(d, inline=True)
        finally:
            os.close(d)
        r = json.loads((self.seat / "broker" / f"{rid}.resp").read_text())
        self.assertEqual(json.loads(base64.b64decode(r["out"]))["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_rr14_the_seat_transcript_is_kept(self):
        proj = self.seat / "claude" / "projects" / "p"
        proj.mkdir(parents=True)
        sid = self.run["session_id"]
        (proj / f"{sid}.jsonl").write_text('{"type":"user"}\n')
        self.assertEqual(seatbroker.seat_transcript(str(self.seat), "claude", sid), str(proj / f"{sid}.jsonl"))
        (proj / f"{sid}.jsonl").unlink()
        (proj / f"{sid}.jsonl").symlink_to(self.tmp / "elsewhere")
        self.assertIsNone(seatbroker.seat_transcript(str(self.seat), "claude", sid))
        rollouts = self.seat / "codex" / "sessions" / "2026" / "10" / "12"
        rollouts.mkdir(parents=True)
        tid = "019a0000-0000-7000-8000-000000000001"
        (rollouts / f"rollout-2026-10-12T10-00-00-{tid}.jsonl").write_text("{}\n")
        self.assertTrue(seatbroker.seat_transcript(str(self.seat), "codex", tid).endswith(f"{tid}.jsonl"))

    def test_rr14_the_runner_copies_it_privately(self):
        proj = self.seat / "claude" / "projects" / "p"
        proj.mkdir(parents=True)
        sid = self.run["session_id"]
        (proj / f"{sid}.jsonl").write_text('{"type":"user"}\n')
        runs = self.tmp / "runs"
        runs.mkdir(mode=0o700)
        run = {"restart_id": 7, "board": None, "seat_home": str(self.seat), "harness": "claude", "session_id": sid}
        import contextlib

        @contextlib.contextmanager
        def runs_fd(board, create=True):
            fd = os.open(runs, os.O_RDONLY)
            try:
                yield fd
            finally:
                os.close(fd)
        with mock.patch.object(runner, "_runs_fd", runs_fd), \
                mock.patch.object(runner, "run_file", lambda rid, board: runs / f"r{rid}.json"), \
                mock.patch.object(runner, "write_run", lambda r: None):
            runner._keep_seat_transcript(run)
        self.assertEqual(Path(run["transcript_copy"]).read_text(), '{"type":"user"}\n')


class Round3Test(BrokerCase):
    def test_rr15_a_requirement_is_read_by_the_broker_and_pinned_at_that_content(self):
        (self.wd / "reqs").mkdir()
        (self.wd / "reqs" / "R.md").write_text("the requirement")
        seen = []

        def runner(argv, **kw):
            for a in cli_argv((argv,)):
                if a.startswith("--add-requirements-pinned="):
                    path, _, f = a[len("--add-requirements-pinned="):].partition("\n")
                    seen.append((path, Path(f).read_text(), f))
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        b = self.broker(runner)
        rc, _, err = self.cli(["goal-ready", "--job", "J", "--candidate", "b@" + "a" * 40,
                               "--add-requirements", "reqs/R.md"], broker=b)
        self.assertEqual(rc, 0, err)
        path, content, f = seen[0]
        self.assertEqual((path, content), (str(self.wd / "reqs" / "R.md"), "the requirement"))
        self.assertTrue(f.startswith(str(self.files)))
        # a link anywhere on the way, or a path outside the work tree, is refused (never read)
        (self.wd / "linked").symlink_to(self.wd / "reqs")
        (self.tmp / "outside.md").write_text("x")
        for p in ("linked/R.md", str(self.tmp / "outside.md")):
            rc, _, err = self.cli(["goal-ready", "--job", "J", "--candidate", "b@" + "a" * 40,
                                   "--add-requirements", p], broker=b)
            self.assertEqual(rc, 2, (p, err))
        self.assertEqual(len(seen), 1)

    def test_rr15_the_cli_pins_a_brokered_requirement(self):
        from swarm import drive_cli
        root = self.wd
        (root / "R.md").write_text("pinned text")
        content = self.files / "c"
        content.write_text("pinned text")
        led = mock.Mock(pinned_requirements={})
        with mock.patch.object(drive_cli, "_owner_roots", lambda bk, job: [str(root)]):
            pins = drive_cli.pins_for("bk", "J", led, [], [f"{root}/R.md\n{content}"])
            self.assertEqual(pins, {f"{root}/R.md": drive_cli.blob_id(b"pinned text")})
            with self.assertRaises(drive_cli.Refuse):
                drive_cli.pins_for("bk", "J", led, [], [f"{self.tmp}/elsewhere.md\n{content}"])
            with self.assertRaises(drive_cli.Refuse):
                drive_cli.pins_for("bk", "J", led, [], [f"{root}/../wt/R.md\n{content}"])

    def test_rr16_a_brokered_review_posts_the_typed_path_and_keeps_the_report(self):
        from swarm import drive_cli
        priv = self.tmp / "priv"
        priv.mkdir(mode=0o700)
        content = self.files / "c"
        content.write_text("the review report")
        board = mock.Mock()
        board.active_agent_name.return_value = "Lisa"
        board.post.return_value = mock.Mock(id=42)
        args = mock.Mock(job="J", key="k", artifact="b@" + "a" * 40, outcome="verified", report="r.md",
                         report_content=str(content))
        with mock.patch("swarm.enrolment.owns", lambda bk, key, job: True), \
                mock.patch.object(drive_cli, "board_key", lambda cfg: "bk"), \
                mock.patch("swarm.supervisor.settings.private_dir", lambda: priv), \
                contextlib.redirect_stdout(io.StringIO()):
            drive_cli.cmd_review(board, {}, args)
            kept = drive_cli.review_report_path("J", 42)
        self.assertEqual(board.post.call_args[0][2], f"REVIEW b@{'a' * 40} VERIFIED r.md")
        self.assertEqual(kept.read_text(), "the review report")
        self.assertTrue(str(kept).startswith(str(priv)))
        rc, _, _ = self.cli(["review", "--job", "J", "verified", "--report", "missing.md"])
        self.assertEqual(rc, 2)    # read in the jail: a missing report never reaches the broker

    def test_rr17_free_text_may_start_with_a_dash(self):
        from swarm import seatproto
        for argv, want in ((["done", "--job", "J", "--summary", "- fixed the parser"], "--summary=- fixed the parser"),
                           (["done", "--job", "J", "--summary=-3 tests flaky"], "--summary=-3 tests flaky"),
                           (["answer", "3", "--comment", "--verbose was wrong"], "--comment=--verbose was wrong"),
                           (["ask", "--job", "J", "--to", "EL", "--options=-a|-b", "pick"], "--options=-a|-b")):
            req = seatproto.parse_cli(argv, str(self.wd))
            built = seatproto.build_argv(req["command"], req["fields"], key="k", name="n", file_path=str)
            self.assertIn(want, built, argv)
        for argv in (["done", "--job=-J"], ["done", "--job", "J", "--artifact=-x"],
                     ["post", "--job", "J\nX", "hi"], ["seat", "request", "--job", "J", "--role", "-r", "--brief", "b"]):
            with self.assertRaises(seatproto.ProtocolError, msg=argv):
                seatproto.parse_cli(argv, str(self.wd))

    def test_rr18_a_linked_directory_is_never_followed(self):
        sid = self.run["session_id"]
        real = self.tmp / "realproj"
        real.mkdir()
        (real / f"{sid}.jsonl").write_text("not the seat's")
        (self.seat / "claude" / "projects").mkdir(parents=True)
        (self.seat / "claude" / "projects" / "p").symlink_to(real)
        self.assertIsNone(seatbroker.read_seat_transcript(str(self.seat), "claude", sid, 1 << 20))
        (self.seat / "claude" / "projects" / "p").unlink()
        (self.seat / "claude" / "projects" / "p").mkdir()
        (self.seat / "claude" / "projects" / "p" / f"{sid}.jsonl").write_text("mine")
        self.assertEqual(seatbroker.read_seat_transcript(str(self.seat), "claude", sid, 1 << 20), b"mine")
        self.assertIsNone(seatbroker.read_under(str(self.seat), "claude/../claude/projects/p/x", 10))



class Rr19Test(BrokerCase):
    """The broker's attestation cannot be set by an agent: only a live record the broker wrote counts."""

    def test_a_bare_variable_attests_nothing(self):
        import secrets
        d = str(self.tmp / "attest")
        for env in ({"SWARM_BROKER_HOST_PID": "4242"}, {person.BROKER_TOKEN_ENV: secrets.token_hex(32)},
                    {person.BROKER_TOKEN_ENV: "not-hex"}):
            self.assertIsNone(person.broker_attested(env, attest_dir=d), env)
        codex = {"CODEX_THREAD_ID": "t", "SWARM_BROKER_HOST_PID": "4242",
                 person.BROKER_TOKEN_ENV: secrets.token_hex(32)}
        with mock.patch.object(person, "broker_attested", lambda env=None, attest_dir=None: None):
            self.assertTrue(person.codex_session(codex))      # still a sandboxed Codex session: CONTROL-REQUEST
        with mock.patch.dict(os.environ, codex):
            self.assertNotEqual(person.host_pid(parent_of=lambda pid: None), 4242)

    def test_only_the_brokers_live_record_counts(self):
        import time as _t
        token, name = self.b._attest()
        d = str(self.tmp / "attest")
        self.assertEqual(person.broker_attested({person.BROKER_TOKEN_ENV: token}, attest_dir=d), 4242)
        self.b._unattest(name)
        person._ATTESTED.clear()
        self.assertIsNone(person.broker_attested({person.BROKER_TOKEN_ENV: token}, attest_dir=d))
        token, name = self.b._attest()
        p = Path(d) / name
        p.write_text(json.dumps({"pid": 4242, "expires_at": _t.time() - 1}))
        person._ATTESTED.clear()
        self.assertIsNone(person.broker_attested({person.BROKER_TOKEN_ENV: token}, attest_dir=d))   # expired
        os.chmod(d, 0o755)
        person._ATTESTED.clear()
        self.assertIsNone(person.broker_attested({person.BROKER_TOKEN_ENV: token}, attest_dir=d))   # not private


class FuzzTest(BrokerCase):
    """Every seat command with extra, abbreviated or identity flags, and every unknown field: refused."""

    def test_every_command(self):
        from swarm import seatproto
        bad_flags = (["--config", "x.toml"], ["--conf=x.toml"], ["--a", "Bart"], ["--ke", "K"], ["--k=K"],
                     ["--sess", "S"], ["--bogus"], ["--plugin-dir", "/x"], ["-c", "x"])
        for command, spec in seatproto.SCHEMA.items():
            base = command.split(" ")
            for flag in bad_flags:
                with self.assertRaises(seatproto.ProtocolError, msg=(command, flag)):
                    seatproto.parse_cli([*base, *flag], str(self.wd))
            for field in ("config", "argv", "env", "as", "key", "session", "unknown_field"):
                with self.assertRaises(seatproto.ProtocolError, msg=(command, field)):
                    seatproto.validate({"v": 2, "kind": "cli", "command": command,
                                        "fields": {**{r: "x" for r in spec.get("required", ())}, field: "x"}})
            for name, (typ, opt) in spec["fields"].items():
                if typ != "str" or opt is None:
                    continue
                fields = {r: (["x"] if spec["fields"][r][0] in ("list", "files", "repeat") else
                              spec["fields"][r][0][1][0] if isinstance(spec["fields"][r][0], tuple) else "x")
                          for r in spec.get("required", ())}
                fields[name] = "--config=/evil"
                with self.assertRaises(seatproto.ProtocolError, msg=(command, name)):
                    seatproto.build_argv(command, seatproto.validate(
                        {"v": 2, "kind": "cli", "command": command, "fields": fields})[1],
                        key="k", name="n", file_path=lambda c: "/f")
        for bad in ({"v": 1, "kind": "cli", "command": "post", "fields": {}},
                    {"v": 2, "kind": "cli", "command": "bg", "fields": {}},
                    {"v": 2, "kind": "cli", "command": "post", "fields": {"job": "J", "message": "x"}},
                    {"v": 2, "kind": "cli", "command": "post", "fields": {"job": "J", "message": ["x"]}, "argv": []}):
            with self.assertRaises(seatproto.ProtocolError):
                seatproto.validate(bad)


EVIL = 'open({marker!r}, "a").write(__name__ + "\\n")\n'


class NoSeatCodeTest(BrokerCase):
    """A brokered CLI (the real one, through BOOT) imports nothing from the seat's files."""

    def setUp(self):
        super().setUp()
        self.marker = self.tmp / "imported"
        for rel in ("swarm/__init__.py", "json.py", "argparse.py", "plugins/evil.py", "evil/plugins/evil.py"):
            p = self.wd / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(EVIL.format(marker=str(self.marker)))
        (self.wd / "evil" / "none.toml").write_text("")
        board = self.tmp / "board"
        board.mkdir()
        self.config = board / "config.toml"
        self.config.write_text(f'[board]\nbackend = "sqlite"\nspool_dir = "{board}/spool"\n'
                               f'[sqlite]\npath = "{board}/board.sqlite3"\n')

    def test_brokered_cli_runs_only_the_supervisors_code(self):
        b = seatbroker.SeatBroker(self.run, str(self.seat / "broker"), workdir=str(self.wd), forbid=[str(self.wd)],
                                  seat_dir=str(self.seat), config=str(self.config), host_pid=None,
                                  base_env={**os.environ, "PYTHONPATH": str(self.wd), "SWARM_AUTO_INIT": "1"},
                                  file_dir=str(self.files))
        rc, out, err = self.cli(["who", "--job", "J"], broker=b)
        self.assertNotEqual(rc, seatbroker.AUDIT_EXIT, err)
        self.assertFalse(self.marker.exists(), self.marker.read_text() if self.marker.exists() else "")

    def test_the_audit_refuses_a_forbidden_module(self):
        b = seatbroker.SeatBroker(self.run, str(self.seat / "broker"), workdir=str(self.wd),
                                  forbid=[seatbroker.LIB_DIR], seat_dir=str(self.seat), config=str(self.config),
                                  host_pid=None, base_env=dict(os.environ), file_dir=str(self.files))
        rc, _, err = b._exec(["who", "--job", "J"], None, 60)
        self.assertEqual(rc, seatbroker.AUDIT_EXIT, err)


FAKE_SEAT = textwrap.dedent("""\
    import json, subprocess, sys
    job, other = sys.argv[1], sys.argv[2]
    def sw(*a):
        r = subprocess.run(["swarm", *a], capture_output=True, text=True)
        return {"rc": r.returncode, "out": r.stdout.strip()[-300:], "err": r.stderr.strip()[-300:]}
    open("report.md", "w").write("all requirements hold")
    print(json.dumps({
        "post": sw("post", "--job", job, "hello from the jail"),
        "forged_as": sw("post", "--job", job, "--as", other, "forged-as"),
        "forged_abbrev": sw("post", "--job", job, "--a", other, "forged-abbrev"),
        "config": sw("post", "--job", job, "--config", "x.toml", "forged-config"),
        "read": sw("read", "--job", job),
        "verdict": sw("verdict", "--job", job, "met", "all requirements hold", "--details", "report.md"),
        "review": sw("review", "--job", job, "--artifact", "b@" + "a" * 40, "verified", "--report", "report.md"),
        "goal_ready": sw("goal-ready", "--job", job, "--candidate", "b@" + "a" * 40, "--add-requirements",
                         "report.md"),
        "dash": sw("post", "--job", job, "- a line that starts with a dash"),
        "bg": sw("bg", "--", "id"),
    }))
    """)


@unittest.skipUnless(CAN_JAIL, "needs unprivileged user namespaces (Linux)")
class BrokerEndToEndTest(SeatHomeCase):
    """A fake-harness seat in the real jail, with no board access of its own, posts, reads and records a
    verdict through the typed broker; who it is comes from its launch record."""

    def test_post_read_verdict(self):
        board_dir = self.tmp / "board"
        board_dir.mkdir()
        config = board_dir / "config.toml"
        config.write_text(f'[board]\nbackend = "sqlite"\nspool_dir = "{board_dir}/spool"\n'
                          f'[sqlite]\npath = "{board_dir}/board.sqlite3"\n[hook]\nmarker_dir = "{board_dir}/m"\n')
        env = {**os.environ, "SWARM_CONFIG": str(config), "PYTHONPATH": str(LIB), "SWARM_AUTO_INIT": "1"}
        for v in ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "CODEX_SESSION_ID", "CLAUDECODE"):
            env.pop(v, None)

        def sw(*a):
            r = subprocess.run([sys.executable, "-m", "swarm.cli", *a], capture_output=True, text=True, env=env,
                               timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            return r.stdout.strip().splitlines()[-1]
        sw("activate", "--job", "T20", "--session", "main-1")
        other = sw("join", "--job", "T20", "--key", "other-key", "--role", "worker")
        sid = str(uuid.uuid4())
        name = sw("join", "--job", "T20", "--key", sid, "--role", "judge", "--judge")
        wt = self.worktree()
        from swarm import cli as swarm_cli, enrolment
        from swarm.board.autoinit import store_key
        enrolment.write(store_key(swarm_cli.load_config(config)), job="T20", agent_key=sid, harness="claude",
                        session_id=sid, cwd=wt)   # what the seat's (brokered) hooks record when it starts
        (Path(wt) / "seat.py").write_text(FAKE_SEAT)
        with mock.patch("swarm.seatjail.available", lambda: (True, "")):
            argv = seathome.wrap(self.cfg, [sys.executable, "-I", "seat.py", "T20", other], job="T20", seat="judge",
                                 workdir=wt)
        sh = str(seathome.home_dir("T20", "judge"))
        files = self.tmp / "files"
        files.mkdir(mode=0o700)
        run = {"harness": "claude", "session_id": sid, "name": name, "cwd": wt}
        broker = seatbroker.SeatBroker(run, f"{sh}/broker", workdir=wt, forbid=[wt], seat_dir=sh, config=str(config),
                                       host_pid=None, base_env=env, file_dir=str(files)).start()
        try:
            r = subprocess.run(argv, capture_output=True, text=True, env={**env, "PGPASSWORD": "never"},
                               timeout=180)
        finally:
            broker.stop()
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertEqual(out["post"]["rc"], 0, out)
        self.assertEqual(out["forged_as"]["rc"], 0, out)          # --as dropped: posted as the seat itself
        self.assertEqual(out["forged_abbrev"]["rc"], 2, out)
        self.assertEqual(out["config"]["rc"], 2, out)
        self.assertEqual(out["read"]["rc"], 0, out)
        self.assertEqual(out["verdict"]["rc"], 0, out)
        self.assertIn("recorded", out["verdict"]["out"])
        self.assertEqual(out["bg"]["rc"], 2, out)
        self.assertEqual(out["dash"]["rc"], 0, out)
        # review and goal-ready reach the CLI with the files' content (no "cannot read", no "outside the job's
        # work roots"): this plain board job has no drive ledger, so the drive commands stop there
        self.assertEqual(out["review"]["rc"], 0, out)          # the report read in the jail, kept privately
        self.assertIn("swarm-record: kind=review", out["review"]["out"])
        kept = list((self.priv / "review-reports").iterdir())
        self.assertEqual([p.read_text() for p in kept], ["all requirements hold"])
        self.assertTrue(any(name in row and f"REVIEW b@{'a' * 40} VERIFIED report.md" in row for row in
                            __import__("sqlite3").connect(board_dir / "board.sqlite3").execute(
                                "select * from messages").fetchall()))
        for k in ("review", "goal_ready"):
            self.assertNotIn("cannot read", out[k]["err"], out)
            self.assertNotIn("outside the job's work roots", out[k]["err"], out)
            self.assertNotIn("refused in a drive seat", out[k]["err"], out)
        import sqlite3
        with sqlite3.connect(board_dir / "board.sqlite3") as c:
            rows = c.execute("select * from messages").fetchall()
        self.assertTrue(any(name in row and "hello from the jail" in row for row in rows), rows)
        self.assertTrue(any(name in row and "forged-as" in row for row in rows), rows)
        self.assertFalse(any(other in row for row in rows if "forged-as" in row))
        self.assertFalse(any("forged-abbrev" in row or "forged-config" in row for row in rows))


class ClientTest(unittest.TestCase):
    def test_no_broker_dir(self):
        with mock.patch.dict(os.environ, {"SWARM_SEAT_BROKER": "/nonexistent"}), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(seatbroker_client.main(["cli", "who", "--job", "J"]), 1)
        self.assertIn("no seat broker", err.getvalue())

    def test_a_dead_broker_times_out(self):
        d = tmpdir(self)
        clock = iter(range(0, 1000, 10))
        self.assertIsNone(seatbroker_client.request({"v": 2}, str(d), 5, clock=lambda: next(clock),
                                                    sleep=lambda s: None))
        self.assertEqual(os.listdir(d), [])     # the unanswered request is withdrawn

class BestEffortTest(SeatHomeCase):
    """macOS and Windows (mocked): no filesystem jail, but the seat's environment carries no credential."""

    def test_mode_by_platform(self):
        self.assertEqual(seathome.mode(self.cfg, "linux"), "required")
        self.assertEqual(seathome.mode(self.cfg, "darwin"), "best-effort")
        self.assertEqual(seathome.mode(self.cfg, "win32"), "best-effort")

    def test_launch_on_macos(self):
        argv, env, m = seathome.launch(self.cfg, ["claude", "-p"], job="J", seat="s", platform="darwin")
        h = seathome.home_dir("J", "s")
        self.assertEqual((argv, m), (["claude", "-p"], "best-effort"))
        self.assertEqual(env["HOME"], str(h / "home"))
        self.assertEqual(env["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertEqual(env["GIT_CONFIG_GLOBAL"], str(h / "gitconfig"))
        self.assertIn("[credential]\n\thelper =\n", (h / "gitconfig").read_text())
        self.assertIn("IdentityAgent=none", env["GIT_SSH_COMMAND"])
        self.assertIn("IdentitiesOnly=yes", env["GIT_SSH_COMMAND"])
        self.assertEqual(env["GH_CONFIG_DIR"], str(h / "gh"))
        self.assertEqual(os.listdir(h / "gh"), [])
        self.assertEqual(env["SWARM_SEAT_BROKER"], str(h / "broker"))
        self.assertNotIn("USERPROFILE", env)
        with mock.patch.object(sys, "platform", "darwin"):
            scripts = seathome.client_scripts()
        self.assertIn(sys.executable, scripts["swarm"])

    def test_launch_on_windows(self):
        _argv, env, _m = seathome.launch(self.cfg, ["claude"], job="J", seat="w", platform="win32")
        h = seathome.home_dir("J", "w")
        self.assertEqual(env["USERPROFILE"], str(h / "home"))
        self.assertTrue(env["APPDATA"].startswith(str(h / "home")))
        self.assertTrue(env["LOCALAPPDATA"].startswith(str(h / "home")))
        scripts = seathome.client_scripts("win32", python="C:\\\\Py\\\\python.exe")
        self.assertIn("swarm.cmd", scripts)
        self.assertIn('"C:\\\\Py\\\\python.exe" -I "%~dp0seatbroker_client.py" cli %*', scripts["swarm.cmd"])

    def test_the_whole_environment(self):
        _argv, over, _m = seathome.launch(self.cfg, ["claude"], job="J", seat="s", platform="darwin")
        base = {"PATH": "/usr/bin", "HOME": str(self.home), "LANG": "C", "SSH_AUTH_SOCK": "/tmp/agent",
                "GH_TOKEN": "x", "PGPASSWORD": "y", "GIT_ASKPASS": "/a", "GIT_CONFIG_GLOBAL": "/evil",
                "SYSTEMROOT": "C:\\\\Windows", "SWARM_RUN_ID": "r1"}
        env = seathome.best_effort_child_env(base, over, str(seathome.home_dir("J", "s")), platform="win32")
        self.assertNotIn("SYSTEMROOT", seathome.best_effort_child_env(base, over, str(seathome.home_dir("J", "s")),
                                                                      platform="darwin"))
        for k in ("SSH_AUTH_SOCK", "GH_TOKEN", "PGPASSWORD", "GIT_ASKPASS"):
            self.assertNotIn(k, env)
        self.assertEqual(env["GIT_CONFIG_GLOBAL"], over["GIT_CONFIG_GLOBAL"])
        self.assertEqual(env["HOME"], over["HOME"])
        self.assertTrue(env["PATH"].startswith(str(seathome.home_dir("J", "s") / "bin") + os.pathsep))
        self.assertEqual(env["SYSTEMROOT"], "C:\\\\Windows")
        self.assertEqual(env["SWARM_RUN_ID"], "r1")

    def test_runner_starts_a_best_effort_seat_and_logs_once(self):
        logged = []
        run = {"argv": ["claude", "-p"], "job": "J", "name": "Lisa", "seat": "eng", "cwd": str(self.work),
               "launch_id": "L1"}
        with mock.patch.object(seathome, "jail_platform", lambda platform=None: False), \
                mock.patch("swarm.supervisor.settings.log", logged.append), \
                mock.patch.object(runner, "_BEST_EFFORT_LOGGED", False):
            a, why = runner._seat_argv(self.cfg, dict(run))
            r2 = dict(run)
            b, _ = runner._seat_argv(self.cfg, r2)
        self.assertIsNone(why)
        self.assertEqual(a, ["claude", "-p"])
        self.assertEqual(r2["seat_mode"], "best-effort")
        self.assertIn("SWARM_SEAT_BROKER", r2["seat_env"])
        self.assertTrue(r2["seat_home"])
        self.assertEqual(len([m for m in logged if "best-effort" in m]), 1)
        self.assertIn("(no filesystem jail)", logged[0])

    def test_doctor_warns(self):
        with mock.patch.object(seathome, "jail_platform", lambda platform=None: False):
            ok, detail = seathome.probe(self.cfg)
        self.assertIsNone(ok)
        self.assertEqual(detail, f"best-effort on {sys.platform} (no filesystem jail)")
        check = bootstrap._seat_isolation_check(self.cfg, probe=lambda c: (None, "best-effort on darwin (no filesystem jail)"))
        self.assertIsNone(check.ok)                     # WARN
        self.assertEqual(check.name, "seat isolation")
        self.assertEqual(check.detail, "best-effort on darwin (no filesystem jail)")

    def test_linux_without_user_namespaces_falls_back_to_best_effort(self):
        with mock.patch.object(seathome, "jail_platform", lambda platform=None: True):
            argv, env, m = seathome.launch(self.cfg, ["claude"], job="J", seat="s", probe=lambda: (False, "no userns"),
                                           no_userns=lambda: "unprivileged user namespaces are disabled (x=0)")
        self.assertEqual((argv, m), (["claude"], "best-effort"))
        self.assertEqual(env["GIT_CONFIG_NOSYSTEM"], "1")
        with mock.patch.object(seathome, "jail_platform", lambda platform=None: True), \
                mock.patch("swarm.seatjail.available", lambda: (False, "no userns")):
            ok, detail = seathome.probe(self.cfg, no_userns=lambda: "unprivileged user namespaces are disabled (x=0)")
        self.assertIsNone(ok)
        self.assertEqual(detail, f"best-effort on {sys.platform} (no filesystem jail): "
                                 "unprivileged user namespaces are disabled (x=0)")
        check = bootstrap._seat_isolation_check(self.cfg, probe=lambda c: (ok, detail))
        self.assertIsNone(check.ok)      # WARN

    def test_strict_refuses_wherever_there_is_no_jail(self):
        self.cfg["drive"]["seat_isolation"] = "strict"
        with mock.patch.object(seathome, "jail_platform", lambda platform=None: True):
            with self.assertRaises(seathome.SeatIsolationError):
                seathome.launch(self.cfg, ["claude"], job="J", seat="s", probe=lambda: (False, "no userns"))
            argv, env, m = seathome.launch(self.cfg, ["claude"], job="J", seat="s", probe=lambda: (True, ""))
            self.assertEqual((env, m), (None, "required"))
            self.assertIn("seatjail.py", argv[2])
        for plat in ("darwin", "win32"):
            with self.assertRaises(seathome.SeatIsolationError):
                seathome.launch(self.cfg, ["claude"], job="J", seat="s", platform=plat)
        with mock.patch.object(seathome, "jail_platform", lambda platform=None: True), \
                mock.patch("swarm.seatjail.available", lambda: (False, "no userns")):
            ok, detail = seathome.probe(self.cfg)
        self.assertEqual((ok, detail), (None, "unavailable: no userns"))
        self.assertFalse(bootstrap._seat_isolation_check(self.cfg, probe=lambda c: (ok, detail)).ok)

    def test_an_unknown_value_is_refused(self):
        self.cfg["drive"]["seat_isolation"] = "maybe"
        with self.assertRaises(seathome.SeatIsolationError):
            seathome.launch(self.cfg, ["claude"], job="J", seat="s", platform="darwin")


class BrokerPathModeTest(BrokerCase):
    """The broker without descriptor-relative calls (Windows), on this host: same answers, links still refused."""

    def setUp(self):
        super().setUp()
        p = mock.patch.object(seatbroker, "FD_MODE", False)
        p.start()
        self.addCleanup(p.stop)
        q = mock.patch.object(seatbroker_client, "_NOFOLLOW", 0)
        q.start()
        self.addCleanup(q.stop)

    def raw(self, body):
        rid = uuid.uuid4().hex
        (self.seat / "broker" / f"{rid}.req").write_text(json.dumps(body))
        self.b.poll_once(str(self.seat / "broker"), inline=True)
        r = json.loads((self.seat / "broker" / f"{rid}.resp").read_text())
        return r["rc"], base64.b64decode(r["out"]), base64.b64decode(r["err"])

    def test_post_and_refusals(self):
        rc, out, _ = self.raw({"v": 2, "kind": "cli", "command": "post", "fields": {"job": "J", "message": ["hi"]}})
        self.assertEqual((rc, out), (0, b"ran\n"))
        self.assertEqual(cli_argv(self.calls[-1]), ["post", "--job=J", f"--key={self.run['session_id']}", "--", "hi"])
        rc, _, _ = self.raw({"v": 2, "kind": "cli", "command": "bg", "fields": {}})
        self.assertEqual(rc, 2)

    def test_links_are_refused(self):
        rid = "b" * 32
        (self.seat / "broker" / f"{rid}.req").symlink_to(self.tmp / "nowhere")
        self.b.poll_once(str(self.seat / "broker"), inline=True)
        self.assertEqual(self.calls, [])
        (self.wd / "d").mkdir()
        (self.wd / "d" / "f").write_text("x")
        (self.wd / "l").symlink_to(self.wd / "d")
        self.assertEqual(seatbroker.read_under(str(self.wd), "d/f", 10), b"x")
        self.assertIsNone(seatbroker.read_under(str(self.wd), "l/f", 10))


class FallbackHardeningTest(SeatHomeCase):
    """Review creds-fallback: blocker 1 (downgrade) and blocker 2 (repo-local credential helper)."""

    def test_poc1_a_transient_probe_failure_refuses(self):
        with mock.patch.object(seathome, "jail_platform", lambda platform=None: True):
            for why in ("TimeoutExpired: probe took 10 s", "seatjail: cannot isolate the seat ([Errno 28] ENOSPC)",
                        "OSError: [Errno 11] EAGAIN", "OSError: [Errno 12] ENOMEM"):
                with self.assertRaises(seathome.SeatIsolationError, msg=why) as cm:
                    seathome.launch(self.cfg, ["claude"], job="J", seat="s", probe=lambda w=why: (False, w),
                                    no_userns=lambda: None)
                self.assertIn("fail closed", str(cm.exception))
            ok, detail = seathome.probe(self.cfg, available=lambda: (False, "TimeoutExpired"), no_userns=lambda: None)
            self.assertEqual((ok, detail), (None, "unavailable: TimeoutExpired"))
            self.assertFalse(bootstrap._seat_isolation_check(self.cfg, probe=lambda c: (ok, detail)).ok)

    def test_only_a_deterministic_no_userns_result_falls_back(self):
        files = {"/proc/sys/kernel/unprivileged_userns_clone": "0"}

        def read(path):
            if path in files:
                return files[path]
            raise FileNotFoundError(path)
        self.assertIn("unprivileged_userns_clone=0", seatjail.no_userns_reason(read=read, run=None))
        files = {"/proc/sys/kernel/apparmor_restrict_unprivileged_userns": "1"}
        self.assertIn("apparmor", seatjail.no_userns_reason(read=read))
        files = {"/proc/sys/user/max_user_namespaces": "0"}
        self.assertIn("max_user_namespaces=0", seatjail.no_userns_reason(read=read))
        files = {}
        rc = lambda code: (lambda argv, **kw: subprocess.CompletedProcess(argv, code, b"", b""))  # noqa: E731
        self.assertIn("EPERM", seatjail.no_userns_reason(read=read, run=rc(3)))
        self.assertIsNone(seatjail.no_userns_reason(read=read, run=rc(4)))     # ENOSPC, EINVAL, ...: not a fixed fact
        self.assertIsNone(seatjail.no_userns_reason(read=read, run=rc(0)))     # it works

        def timeout(argv, **kw):
            raise subprocess.TimeoutExpired(argv, 10)
        self.assertIsNone(seatjail.no_userns_reason(read=read, run=timeout))
        if CAN_JAIL:
            self.assertIsNone(seatjail.no_userns_reason())                   # this host has them

    def test_poc2_a_repo_local_credential_helper_never_runs(self):
        _argv, over, _m = seathome.launch(self.cfg, ["claude"], job="J", seat="s", platform="darwin")
        env = seathome.best_effort_child_env({"PATH": os.environ["PATH"], "HOME": str(self.home)}, over,
                                             str(seathome.home_dir("J", "s")))
        repo = self.work / "r"
        self.git("init", "-q", str(repo))
        marker = self.tmp / "helper-ran"
        subprocess.run(["git", "-C", str(repo), "config", "credential.helper",
                        f"!f() {{ echo ran > {marker}; echo password=leaked; }}; f"], check=True)
        r = subprocess.run(["git", "-C", str(repo), "credential", "fill"], input="protocol=https\nhost=example.test\n\n",
                           capture_output=True, text=True, env=env, timeout=30)
        self.assertFalse(marker.exists(), r.stdout)
        self.assertNotIn("leaked", r.stdout)
        helpers = subprocess.run(["git", "-C", str(repo), "config", "--get-all", "credential.helper"],
                                 capture_output=True, text=True, env=env).stdout.splitlines()
        self.assertEqual(helpers[-1], "")       # the command-scope empty value comes last and resets the list
        self.assertEqual(env["GIT_CONFIG_KEY_0"], "credential.helper")
        self.assertEqual(env["GIT_CONFIG_VALUE_0"], "")
        self.assertIn("IdentityAgent=none", env["GIT_CONFIG_VALUE_2"])

    def test_the_jail_pins_the_same_overrides(self):
        h = seathome.prepare(self.cfg, "J", "s")
        env = seathome.jail_spec(self.cfg, h, None, self.home)["env"]
        self.assertEqual((env["GIT_CONFIG_KEY_0"], env["GIT_CONFIG_VALUE_0"]), ("credential.helper", ""))

    def test_windows_names_only_on_windows(self):
        env = {"TEMP": "x", "COMSPEC": "y", "PATH": "/bin"}
        self.assertEqual(seatjail.scrub_env(env, "linux"), {"PATH": "/bin"})
        self.assertEqual(seatjail.scrub_env(env, "darwin"), {"PATH": "/bin"})
        self.assertEqual(seatjail.scrub_env(env, "win32"), env)

    def test_every_linux_fallback_is_logged_with_its_reason(self):
        logged = []
        run = {"argv": ["claude"], "job": "J", "name": "L", "seat": "eng", "cwd": str(self.work), "launch_id": "L1"}
        with mock.patch("swarm.seatjail.available", lambda: (False, "no userns")), \
                mock.patch("swarm.seatjail.no_userns_reason", lambda: "unprivileged user namespaces are disabled (k=0)"), \
                mock.patch.object(seathome, "jail_platform", lambda platform=None: True), \
                mock.patch("swarm.supervisor.settings.log", logged.append):
            runner._seat_argv(self.cfg, dict(run))
            runner._seat_argv(self.cfg, dict(run))
        self.assertEqual(len(logged), 2)
        self.assertIn("(k=0)", logged[0])


if __name__ == "__main__":
    unittest.main()
