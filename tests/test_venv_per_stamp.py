"""The launcher's venv: one per requirements stamp (venv-<stamp>), and a build lock that a killed
build cannot leave hanging. bin/swarm (POSIX sh) is run for real against a fake `python3` that
"builds" a fake venv, so nothing touches pip or the network; lib/swarm/winlaunch.py and paths.py
are tested directly."""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from support import ROOT, home_env, posix_only  # noqa: F401  (puts lib/ on sys.path)

from swarm import paths, winlaunch

FAKE_PYTHON3 = """#!/bin/sh
# a fake python3: a version check passes, pgwant says no Postgres, `-m venv DIR` builds a fake venv
case "$1" in
  -c) exit 0;;
  -I) exit 1;;
esac
if [ "$1 $2" = "-m venv" ]; then
  d="$3"
  mkdir -p "$d/bin" "$d/lib/python3.12/site-packages/zstandard"
  : > "$d/lib/python3.12/site-packages/zstandard/__init__.py"
  cat > "$d/bin/python" <<'PY'
#!/bin/sh
echo "$0 $*" >> "$SWARM_TEST_LOG"
exit 0
PY
  chmod +x "$d/bin/python"
  # FAKE_STEAL_LOCK: while "building", another launcher takes over the lock (a stale-lock steal)
  [ -n "$FAKE_STEAL_LOCK" ] && echo "999999 someone-else" > "$d.building/owner"
  echo "venv $d" >> "$SWARM_TEST_LOG"
  exit 0
fi
exit 0
"""


def cksum_of(*files: Path) -> str:
    data = b"".join(f.read_bytes() for f in files)
    return subprocess.run(["cksum"], input=data, capture_output=True).stdout.split()[0].decode()


class Plugin:
    """A copy of the launcher with requirements of its own (so its stamp differs from another's)."""

    def __init__(self, root: Path, requirements: str):
        (root / "bin").mkdir(parents=True)
        (root / "lib/swarm").mkdir(parents=True)
        shutil.copy(ROOT / "bin/swarm", root / "bin/swarm")
        shutil.copy(ROOT / "lib/swarm/pgwant.py", root / "lib/swarm/pgwant.py")
        (root / "requirements.txt").write_text(requirements)
        (root / "requirements-postgres.txt").write_text("# no postgres\n")
        self.root = root
        self.stamp = cksum_of(root / "requirements.txt", root / "requirements-postgres.txt")


@posix_only("runs bin/swarm, a POSIX sh script (the Windows launcher is tested below)")
class LauncherVenvTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="swarm-venv-", dir=os.environ.get("TMPDIR")))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        fakebin = self.home / "fakebin"
        fakebin.mkdir()
        (fakebin / "python3").write_text(FAKE_PYTHON3)
        (fakebin / "python3").chmod(0o755)
        self.log = self.home / "log"
        self.env = {**home_env(self.home), "PATH": f"{fakebin}:{os.environ['PATH']}",
                    "SWARM_CONFIG": str(self.home / "none.toml"), "SWARM_TEST_LOG": str(self.log),
                    "SWARM_PYCACHE": str(self.home / "pyc")}
        self.env.pop("SWARM_VENV", None)
        self.share = self.home / ".local/share/swarm"
        self.share.mkdir(parents=True)
        self.v22 = Plugin(self.home / "p022", "zstandard==1.0\n")
        self.v23 = Plugin(self.home / "p023", "zstandard==2.0\nextra==1\n")

    def launch(self, plugin: Plugin, *args, timeout=60, **env):
        return subprocess.run([str(plugin.root / "bin/swarm"), *(args or ("status",))], capture_output=True,
                              text=True, timeout=timeout, env={**self.env, **env})

    def venv_of(self, plugin: Plugin) -> Path:
        return self.share / f"venv-{plugin.stamp}"

    def lock_of(self, plugin: Plugin) -> Path:
        return Path(str(self.venv_of(plugin)) + ".building")

    def lines(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    # ---- one venv per requirements stamp
    def test_two_plugin_versions_get_their_own_venvs_and_never_rebuild_each_other(self):
        self.assertNotEqual(self.v22.stamp, self.v23.stamp)
        for p in (self.v22, self.v23):
            res = self.launch(p)
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertTrue((self.venv_of(p) / "bin/python").exists())
            self.assertEqual((self.venv_of(p) / ".swarm-requirements").read_text().strip(), p.stamp)
        self.assertFalse((self.share / "venv").exists(), "the shared venv is not used any more")
        built = [ln for ln in self.lines() if ln.startswith("venv ")]
        self.assertEqual(len(built), 2)
        self.log.unlink()
        for p in (self.v22, self.v23, self.v22):             # alternate: nothing is rebuilt
            res = self.launch(p, "status")
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertNotIn("setting up", res.stderr)
            self.assertEqual(self.lines()[-1], f"{self.venv_of(p)}/bin/python -m swarm.cli status")
        self.assertEqual([ln for ln in self.lines() if " pip " in ln or ln.startswith("venv ")], [])

    def test_an_old_shared_venv_with_a_matching_stamp_is_adopted_not_rebuilt(self):
        legacy = self.share / "venv"
        (legacy / "bin").mkdir(parents=True)
        (legacy / "bin/python").write_text('#!/bin/sh\necho "$0 $*" >> "$SWARM_TEST_LOG"\n')
        (legacy / "bin/python").chmod(0o755)
        (legacy / "lib/python3.12/site-packages/zstandard").mkdir(parents=True)
        (legacy / "lib/python3.12/site-packages/zstandard/__init__.py").write_text("")
        (legacy / ".swarm-requirements").write_text(self.v22.stamp + "\n")
        res = self.launch(self.v22)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(self.lines(), [f"{legacy}/bin/python -m swarm.cli status"])
        self.assertFalse(self.venv_of(self.v22).exists())
        # the other version's stamp differs: it gets its own venv and leaves the old one alone
        res = self.launch(self.v23)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue(self.venv_of(self.v23).exists())
        self.assertEqual((legacy / ".swarm-requirements").read_text().strip(), self.v22.stamp)

    def test_swarm_venv_is_used_as_it_is(self):
        explicit = self.home / "mine"
        res = self.launch(self.v22, SWARM_VENV=str(explicit))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue((explicit / "bin/python").exists())
        self.assertFalse(self.venv_of(self.v22).exists())

    def hook_venv(self, plugin: Plugin) -> str:
        """The $V bin/swarm-hook chooses: its selection lines, run on their own."""
        hook = plugin.root / "bin/swarm-hook"
        shutil.copy(ROOT / "bin/swarm-hook", hook)
        script = f'D="{plugin.root}"\n{_v_selection(hook)}\nprintf %s "$V"\n'
        res = subprocess.run(["sh", "-c", script], capture_output=True, text=True, env=self.env)
        self.assertEqual(res.returncode, 0, res.stderr)
        return res.stdout

    def fake_python(self, venv: Path, stamp: str | None = None) -> None:
        (venv / "bin").mkdir(parents=True)
        (venv / "bin/python").write_text("#!/bin/sh\n")
        (venv / "bin/python").chmod(0o755)
        if stamp:
            (venv / ".swarm-requirements").write_text(stamp + "\n")

    def test_hook_picks_the_same_venv_as_the_launcher(self):
        per, legacy = self.venv_of(self.v22), self.share / "venv"
        self.assertEqual(self.hook_venv(self.v22), str(per), "nothing built yet: the per-stamp path")
        self.fake_python(legacy, self.v22.stamp)
        self.assertEqual(self.hook_venv(self.v22), str(legacy), "an old venv with this stamp is adopted")
        self.assertEqual(self.hook_venv(self.v23), str(self.venv_of(self.v23)), "another stamp: its own")
        self.fake_python(per)
        self.assertEqual(self.hook_venv(self.v22), str(per), "the per-stamp venv wins once it exists")
        self.env["SWARM_VENV"] = str(self.home / "mine")
        self.assertEqual(self.hook_venv(self.v22), str(self.home / "mine"))

    # ---- the build lock
    def hold_lock(self, plugin: Plugin, owner: str | None, *, age_minutes: float = 0) -> Path:
        lock = self.lock_of(plugin)
        lock.mkdir()
        if owner is not None:
            (lock / "owner").write_text(owner + "\n")
        if age_minutes:
            past = time.time() - age_minutes * 60
            os.utime(lock, (past, past))
        return lock

    def dead_pid(self) -> int:
        p = subprocess.Popen(["true"])
        p.wait()
        return p.pid

    def assert_builds_through_a_stale_lock(self, lock: Path):
        res = self.launch(self.v22, timeout=30)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("removed a stale build lock", res.stderr)
        self.assertTrue((self.venv_of(self.v22) / ".swarm-requirements").exists(), "rebuilt after the retry")
        self.assertFalse(lock.exists(), "the lock is released at the end")
        self.assertEqual([p.name for p in self.share.glob("*.stale.*")], [], "the renamed stale lock is gone")

    def test_lock_of_a_dead_owner_is_removed_and_the_build_retried(self):
        lock = self.hold_lock(self.v22, f"{self.dead_pid()} {socket.gethostname()}")
        self.assert_builds_through_a_stale_lock(lock)

    def test_old_lock_of_a_live_owner_is_removed(self):
        lock = self.hold_lock(self.v22, f"{os.getpid()} {socket.gethostname()}", age_minutes=45)
        self.assert_builds_through_a_stale_lock(lock)

    def test_lock_with_no_owner_record_goes_stale_by_age(self):
        lock = self.hold_lock(self.v22, None, age_minutes=45)
        self.assert_builds_through_a_stale_lock(lock)

    def test_age_limit_is_configurable(self):
        lock = self.hold_lock(self.v22, f"{os.getpid()} {socket.gethostname()}", age_minutes=5)
        res = self.launch(self.v22, timeout=30, SWARM_LOCK_STALE_MINUTES="3")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("removed a stale build lock", res.stderr)
        self.assertFalse(lock.exists())

    def assert_waits_on_a_live_lock(self, lock: Path):
        with self.assertRaises(subprocess.TimeoutExpired):
            self.launch(self.v22, timeout=3)
        self.assertTrue(lock.is_dir(), "a live lock is left alone")
        self.assertFalse((self.venv_of(self.v22) / ".swarm-requirements").exists(), "no build while it is held")

    def test_fresh_lock_of_a_live_owner_is_respected(self):
        self.assert_waits_on_a_live_lock(self.hold_lock(self.v22, f"{os.getpid()} {socket.gethostname()}"))

    def test_dead_looking_pid_on_another_host_is_not_stale_while_fresh(self):
        self.assert_waits_on_a_live_lock(self.hold_lock(self.v22, f"{self.dead_pid()} some-other-host"))

    def test_fresh_lock_with_no_owner_record_is_respected(self):
        self.assert_waits_on_a_live_lock(self.hold_lock(self.v22, None))

    def test_the_other_versions_lock_does_not_block_this_one(self):
        self.hold_lock(self.v23, f"{os.getpid()} {socket.gethostname()}")
        res = self.launch(self.v22, timeout=30)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue(self.lock_of(self.v23).is_dir())

    def test_a_lock_taken_over_meanwhile_is_not_released_by_the_old_owner(self):
        res = self.launch(self.v22, timeout=30, FAKE_STEAL_LOCK="1")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual((self.lock_of(self.v22) / "owner").read_text().strip(), "999999 someone-else")

    def test_a_failed_build_releases_its_lock(self):
        (self.home / "fakebin/python3").write_text('#!/bin/sh\ncase "$1" in -c) exit 0;; -I) exit 1;; esac\nexit 1\n')
        res = self.launch(self.v22, timeout=30)
        self.assertEqual(res.returncode, 1)
        self.assertIn("could not set up", res.stderr)
        self.assertFalse(self.lock_of(self.v22).exists())


def _v_selection(hook: Path) -> str:
    """The lines of bin/swarm-hook that choose $V (from `if [ -n "${SWARM_VENV` to its `fi`)."""
    lines = hook.read_text().splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith('if [ -n "${SWARM_VENV:-}" ]'))
    end = next(i for i in range(start, len(lines)) if lines[i] == "fi")
    return "\n".join(lines[start:end + 1])


class StampTests(unittest.TestCase):
    def test_posix_cksum_matches_the_cksum_tool(self):
        if shutil.which("cksum") is None:
            self.skipTest("no cksum tool")
        for data in (b"", b"a", b"zstandard==1.0\n", bytes(range(256)) * 5, b"x" * 70000):
            want = subprocess.run(["cksum"], input=data, capture_output=True).stdout.split()[0].decode()
            self.assertEqual(str(paths.posix_cksum(data)), want, data[:20])

    @posix_only("the POSIX stamp is cksum's; Windows uses winlaunch's CRC-32")
    def test_paths_venv_dir_is_per_stamp_and_matches_the_launcher_stamp(self):
        home = Path(tempfile.mkdtemp(prefix="swarm-venvdir-", dir=os.environ.get("TMPDIR")))
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        env = {**home_env(home)}
        with mock.patch.dict(os.environ, env):
            os.environ.pop("SWARM_VENV", None)
            want = cksum_of(ROOT / "requirements.txt", ROOT / "requirements-postgres.txt")
            self.assertEqual(paths.venv_dir(), home / ".local/share/swarm" / f"venv-{want}")
            os.environ["SWARM_VENV"] = str(home / "x")
            self.assertEqual(paths.venv_dir(), home / "x")


class WinLaunchVenvTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="swarm-winvenv-", dir=os.environ.get("TMPDIR")))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        env = mock.patch.dict(os.environ, {"HOME": str(self.tmp), "USERPROFILE": str(self.tmp)})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("SWARM_VENV", None)

    def test_default_venv_is_per_stamp(self):
        self.assertEqual(winlaunch.venv_dir(),
                         self.tmp / ".local/share/swarm" / f"venv-{winlaunch.requirements_stamp()}")

    def test_an_old_venv_with_a_matching_stamp_is_adopted_unless_the_new_one_exists(self):
        share = self.tmp / ".local/share/swarm"
        legacy = share / "venv"
        winlaunch.venv_python(legacy).parent.mkdir(parents=True)
        winlaunch.venv_python(legacy).write_text("")
        (legacy / ".swarm-requirements").write_text(winlaunch.requirements_stamp() + "\n")
        self.assertEqual(winlaunch.venv_dir(), legacy)
        per = share / f"venv-{winlaunch.requirements_stamp()}"
        winlaunch.venv_python(per).parent.mkdir(parents=True)
        winlaunch.venv_python(per).write_text("")
        self.assertEqual(winlaunch.venv_dir(), per)
        (legacy / ".swarm-requirements").write_text("stale\n")
        shutil.rmtree(per)
        self.assertEqual(winlaunch.venv_dir(), per, "a legacy venv of another stamp is never adopted")

    def test_stale_lock_is_removed_and_taken(self):
        lock = self.tmp / "venv.building"
        lock.mkdir()
        old = time.time() - 3600
        os.utime(lock, (old, old))
        winlaunch.take_lock(lock, timeout=2)
        self.assertTrue(lock.is_dir())
        self.assertEqual((lock / "owner").read_text().strip(), str(os.getpid()))
        self.assertEqual([p.name for p in self.tmp.glob("venv.building.stale.*")], [])

    def test_fresh_lock_is_waited_on_then_reported(self):
        lock = self.tmp / "venv.building"
        lock.mkdir()
        with mock.patch.object(winlaunch.time, "sleep") as sleep:
            with self.assertRaises(SystemExit) as cm:
                winlaunch.take_lock(lock, timeout=2)
        self.assertEqual(sleep.call_count, 2, "waits once per second up to the timeout")
        self.assertIn("is held", str(cm.exception))
        self.assertTrue(lock.is_dir())

    def test_stale_limit_is_configurable(self):
        lock = self.tmp / "venv.building"
        lock.mkdir()
        past = time.time() - 5 * 60
        os.utime(lock, (past, past))
        self.assertFalse(winlaunch.lock_is_stale(lock))
        with mock.patch.dict(os.environ, {"SWARM_LOCK_STALE_MINUTES": "3"}):
            self.assertTrue(winlaunch.lock_is_stale(lock))

    def test_ensure_venv_releases_its_lock(self):
        v = self.tmp / "v"
        calls = []
        def run(cmd, **kw):
            calls.append(Path(str(v) + ".building").is_dir())
            winlaunch.venv_python(v).parent.mkdir(parents=True, exist_ok=True)
            winlaunch.venv_python(v).write_text("")
            return mock.Mock(returncode=0)
        with mock.patch.object(winlaunch.subprocess, "run", run), \
                mock.patch.object(winlaunch, "want_postgres", return_value=False):
            winlaunch.ensure_venv(v)
        self.assertTrue(all(calls))
        self.assertFalse(Path(str(v) + ".building").exists())


if __name__ == "__main__":
    unittest.main()
