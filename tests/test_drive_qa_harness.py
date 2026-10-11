"""The drive-mode QA harness itself (e2e/drive_live/): the parts that run today, so a broken harness is
caught here and not on the day the driver lands. Covers the pending-probe, the run-dir guards, the fake
forge (file:// bare repos: exact-SHA CI, fast-forward-only main, no merge commits), the scripted engineer's
toy change, and the no-driver chaos layers (corrupt ledger, stale lock, forged receipt) in a subprocess.

Run: cd tests && /var/tmp/test-slots/test-slot -- python3 -B -m unittest test_drive_qa_harness
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401  (puts lib/ on sys.path)

HARNESS = Path(__file__).resolve().parent.parent / "e2e" / "drive_live"
sys.path.insert(0, str(HARNESS))

POSIX = sys.platform != "win32"
HAVE_MAKE = shutil.which("make") is not None and shutil.which("git") is not None


def git(*a, cwd=None):
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"}
    r = subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True, env=env)
    return r


class Tmp(unittest.TestCase):
    def setUp(self):
        # under the home directory, not /tmp: the harness refuses /tmp (tmpfs is RAM on the coder box)
        base = Path.home() / ".cache" / "swarm-qa-harness-tests"
        base.mkdir(parents=True, exist_ok=True)
        self.tmp = Path(tempfile.mkdtemp(prefix="qa-harness-", dir=base))
        self.addCleanup(shutil.rmtree, self.tmp, True)


class Probes(Tmp):
    def test_every_probe_is_absent_in_an_empty_tree_and_pending_names_them(self):
        import qa_lib as q
        caps = q.capabilities(self.tmp)
        self.assertFalse(any(c.present for c in caps.values()))
        with self.assertRaises(q.Pending) as cm:
            q.require("8a", "8b", "8c", "11b", root=self.tmp)
        self.assertEqual(str(cm.exception), "pending 8a/8b/8c/11b")
        self.assertEqual(cm.exception.missing, ["8a", "8b", "8c", "11b"])

    def test_a_probe_turns_on_when_its_marker_text_appears(self):
        import qa_lib as q
        d = self.tmp / "lib/swarm/supervisor"
        d.mkdir(parents=True)
        (d / "driver.py").write_text("def claim_drive_launch(): pass\n")
        caps = q.capabilities(self.tmp)
        self.assertTrue(caps["8a"].present)
        self.assertFalse(caps["8b"].present)      # needs terminalization and the core-sweep skip too
        q.require("8a", root=self.tmp)
        with self.assertRaisesRegex(q.Pending, "pending 8b"):
            q.require("8a", "8b", root=self.tmp)

    def test_this_checkout_has_the_ledger_and_receipts_the_chaos_layers_need(self):
        import qa_lib as q
        caps = q.capabilities(Path(__file__).resolve().parent.parent)
        self.assertTrue(caps["ledger"].present and caps["receipts"].present)


class Guards(Tmp):
    def test_run_dir_must_be_under_the_qa_root_and_never_tmp(self):
        import qa_lib as q
        with self.assertRaises(q.Refused):
            q.check_run_dir(Path("/tmp/x"))
        with self.assertRaises(q.Refused):
            q.check_run_dir(Path("/var/tmp/x"))
        with self.assertRaises(q.Refused):
            q.check_run_dir(Path(os.path.expanduser("~")) / "elsewhere")
        self.assertEqual(q.check_run_dir(q.QA_ROOT / "runs" / "x"), q.QA_ROOT / "runs" / "x")

    def test_job_names_are_plain(self):
        import qa_lib as q
        for bad in ("a b", "../x", "", "x" * 80, "a;b"):
            with self.assertRaises(q.Refused):
                q.check_job_name(bad)
        self.assertEqual(q.check_job_name("qa-live.met_1"), "qa-live.met_1")


@unittest.skipUnless(POSIX and HAVE_MAKE, "the fake forge needs git, make and POSIX sh hooks")
class Forge(Tmp):
    def setUp(self):
        super().setUp()
        import forge
        self.forge = forge
        self.f = self.tmp / "forge"
        info = forge.init(self.f, HARNESS / "toy-repo-template", ["origin", "backup"])
        self.initial = info["initial"]
        self.urls = {k: v for k, v in info.items() if k != "initial"}
        self.w = self.tmp / "w"
        git("clone", "-q", self.urls["origin"], str(self.w))

    def commit(self, name="x.txt", text="x\n", msg="change"):
        (self.w / name).write_text(text)
        git("add", "-A", cwd=self.w)
        return git("commit", "-q", "-m", msg, cwd=self.w)

    def test_urls_are_file_urls_and_both_destinations_start_equal(self):
        self.assertTrue(all(u.startswith("file:///") for u in self.urls.values()))
        a = self.forge.audit(self.f, None)
        self.assertTrue(a["destinations_equal"] and a["linear"])

    def test_ci_is_green_only_for_the_exact_sha_that_was_run(self):
        f = self.forge
        self.assertEqual(f.ci_state(self.f, self.initial), "none")
        self.assertEqual(f.ci_run(self.f, self.initial)["state"], "green")
        self.assertEqual(f.ci_state(self.f, self.initial), "green")
        self.assertEqual(f.ci_state(self.f, "0" * 40), "none")
        self.assertEqual(f.ci_state(self.f, "abc"), "none")            # not an exact SHA

    def test_a_red_toy_change_makes_ci_red_and_forced_red_sticks(self):
        f = self.forge
        self.commit("tests/test_bad.py", "import unittest\nclass T(unittest.TestCase):\n"
                    "    def test_x(self):\n        self.assertEqual(1, 2)\n")
        sha = git("rev-parse", "HEAD", cwd=self.w).stdout.strip()
        self.assertEqual(git("push", "-q", self.urls["origin"], "HEAD:refs/heads/feat/bad", cwd=self.w).returncode, 0)
        self.assertEqual(f.ci_run(self.f, sha)["state"], "failed")
        f.ci_run(self.f, self.initial)
        (f.results_dir(self.f) / f"{self.initial}.json").write_text(json.dumps({"state": "failed"}))
        self.assertEqual(f.ci_state(self.f, self.initial), "failed")

    def test_main_takes_fast_forwards_only(self):
        self.commit()
        ok = git("push", self.urls["origin"], "HEAD:main", cwd=self.w)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.forge.advance(self.f, "origin")                       # the target moves under us
        self.commit("y.txt", "y\n", "second")
        git("reset", "-q", "--hard", "HEAD", cwd=self.w)
        self.assertNotEqual(git("push", self.urls["origin"], "HEAD:main", cwd=self.w).returncode, 0)   # client side
        bad = git("push", "-f", self.urls["origin"], "HEAD:main", cwd=self.w)                         # the forge itself
        self.assertNotEqual(bad.returncode, 0)
        self.assertRegex(bad.stderr, r"non-fast-forward")

    def test_a_merge_commit_on_main_is_refused(self):
        git("checkout", "-q", "-b", "side", cwd=self.w)
        self.commit("s.txt", "s\n", "side")
        git("checkout", "-q", "main", cwd=self.w)
        self.commit("m.txt", "m\n", "main")
        m = git("merge", "--no-ff", "-q", "-m", "merge", "side", cwd=self.w)
        self.assertEqual(m.returncode, 0, m.stderr)
        bad = git("push", self.urls["origin"], "HEAD:main", cwd=self.w)
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("merge commit", bad.stderr)

    def test_audit_reports_a_merge_commit_that_got_onto_main_by_other_means(self):
        git("checkout", "-q", "-b", "side", cwd=self.w)
        self.commit("s.txt", "s\n", "side")
        git("checkout", "-q", "main", cwd=self.w)
        self.commit("m.txt", "m\n", "main")
        git("merge", "--no-ff", "-q", "-m", "merge", "side", cwd=self.w)
        git("push", "-q", self.urls["origin"], "HEAD:refs/heads/staged", cwd=self.w)   # not main: the hook lets it by
        sha = git("rev-parse", "HEAD", cwd=self.w).stdout.strip()
        git("--git-dir", str(self.f / "origin.git"), "update-ref", "refs/heads/main", sha)   # as an admin would
        self.assertFalse(self.forge.audit(self.f, self.initial)["linear"])

    def test_branch_deletion_is_allowed_for_the_integrator(self):
        self.commit()
        git("push", "-q", self.urls["origin"], "HEAD:refs/heads/feat/a", cwd=self.w)
        gone = git("push", self.urls["origin"], ":refs/heads/feat/a", cwd=self.w)
        self.assertEqual(gone.returncode, 0, gone.stderr)

    def test_advance_moves_every_destination_to_one_commit_and_audit_sees_it(self):
        moved = self.forge.advance(self.f, None)
        self.assertEqual(len(set(moved.values())), 1)
        a = self.forge.audit(self.f, self.initial)
        self.assertTrue(a["destinations_equal"] and a["linear"])
        self.assertEqual(a["commits"], list(set(moved.values())))

    def test_the_cli_ci_check_is_an_evidence_command(self):
        script = HARNESS / "forge.py"
        r = subprocess.run([sys.executable, str(script), "ci-check", "--forge", str(self.f), "--run", self.initial],
                           capture_output=True, text=True)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "green"))
        r = subprocess.run([sys.executable, str(script), "ci-check", "--forge", str(self.f), "f" * 40],
                           capture_output=True, text=True)
        self.assertEqual((r.returncode, r.stdout.strip()), (1, "none"))


@unittest.skipUnless(POSIX and HAVE_MAKE, "needs git, make and POSIX")
class ScriptedEngineer(Tmp):
    """The fake engineer's change is real work: the version change passes the toy's tests, the impossible
    one cannot."""

    def load_harness(self):
        run = self.tmp / "run"
        (run / "home").mkdir(parents=True)
        script = run / "s.json"
        script.write_text("{}")
        (run / "harness.json").write_text(json.dumps({"repo": str(HARNESS), "script": str(script),
                                                      "forge": str(run / "forge")}))
        old = os.environ.get("HOME")
        os.environ["HOME"] = str(run / "home")
        self.addCleanup(lambda: os.environ.__setitem__("HOME", old) if old else os.environ.pop("HOME", None))
        sys.modules.pop("fake_harness", None)
        import fake_harness
        return fake_harness

    def toy(self):
        w = self.tmp / "toy"
        shutil.copytree(HARNESS / "toy-repo-template", w)
        return w

    def make_test(self, w):
        return subprocess.run(["make", "test"], cwd=w, capture_output=True, text=True,
                              env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})

    def test_version_change_passes_make_test_and_adds_a_real_test(self):
        fh, w = self.load_harness(), self.toy()
        self.assertEqual(self.make_test(w).returncode, 0)
        fh.edit_toy(w, "version", 0)
        r = self.make_test(w)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("test_version", (w / "tests" / "test_toy.py").read_text())
        self.assertEqual(subprocess.run([sys.executable, "-B", "toy.py", "--version"], cwd=w, capture_output=True,
                                        text=True).stdout.strip(), "toy 0.1.0")

    def test_a_fix_round_adds_another_test_that_still_passes(self):
        fh, w = self.load_harness(), self.toy()
        fh.edit_toy(w, "version", 0)
        fh.edit_toy(w, "version", 1)
        self.assertEqual(self.make_test(w).returncode, 0)
        self.assertIn("Round1", (w / "tests" / "test_toy.py").read_text())

    def test_the_impossible_change_fails_make_test(self):
        fh, w = self.load_harness(), self.toy()
        fh.edit_toy(w, "impossible", 0)
        self.assertNotEqual(self.make_test(w).returncode, 0)

    def test_the_nth_script_entry_sticks_at_the_last(self):
        fh = self.load_harness()
        self.assertEqual(fh.nth([{"a": 1}, {"a": 2}], 0), {"a": 1})
        self.assertEqual(fh.nth([{"a": 1}, {"a": 2}], 5), {"a": 2})
        self.assertEqual(fh.nth({"a": 9}, 3), {"a": 9})
        self.assertEqual(fh.nth(None, 0, "d"), "d")


@unittest.skipUnless(POSIX, "POSIX only (O_NOFOLLOW ledger)")
class NoDriverChaosLayers(Tmp):
    """The `<name>/today` rows of the chaos suite, in a subprocess (they replace $HOME and the environment
    of their process) on a throwaway SQLite board under a temp QA root. No Postgres, no driver
    (`--today-only` leaves the driven rows to e2e/drive_live.sh)."""

    def run_chaos(self, *names):
        env = {k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "LC_ALL")}
        env.update(SWARM_QA_ROOT=str(self.tmp / "qa"), SWARM_QA_VENV=sys.prefix,
                   SWARM_QA_REPO=str(Path(__file__).resolve().parent.parent))
        return subprocess.run([sys.executable, "-B", str(HARNESS / "live.py"), "chaos", "--today-only", "--backend", "sqlite", *names],
                              capture_output=True, text=True, env=env, cwd=HARNESS, timeout=240)

    def test_corrupt_ledger_stale_lock_and_forged_receipt_layers_pass(self):
        if not (Path(sys.prefix) / "bin" / "python").exists():
            self.skipTest("no venv python under sys.prefix for the run shims")
        r = self.run_chaos("corrupt_ledger", "stale_lock", "forged_receipt")
        out = r.stdout
        for name in ("corrupt_ledger/today", "stale_lock/today", "forged_receipt/today"):
            self.assertRegex(out, rf"PASS\s+{name}\b", out + r.stderr[-1500:])
        self.assertNotRegex(out, r"(?m)^FAIL\s")
        self.assertEqual(r.returncode, 0, out)

    def test_an_unknown_scenario_is_a_failure_not_a_silent_skip(self):
        r = self.run_chaos("no_such_scenario")
        self.assertEqual(r.returncode, 1)
        self.assertIn("unknown scenario", r.stdout)


if __name__ == "__main__":
    unittest.main()
