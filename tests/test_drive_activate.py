"""Task 6a: `swarm activate` in drive mode. A goal implies drive ([A]); the supervisor must be enabled,
its timer running and a pass recent, on a platform with a timer ([M1]); activation is five durable steps
that the next pass completes after any crash ([RR1]); budgets and models are configurable ([D1], [P1]);
credentials are pinned from an allowlist ([G2]); `swarm supervise disable|enable` halts and unhalts the
drive ledgers ([F5]); `swarm drive budget|models` change a running job without a rollover. Unit cases
run under a temporary HOME in the suite sandbox; the CLI cases use the board of test_hooks_cli.Env."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from support import SANDBOX, base_config, home_env, posix_only  # noqa: F401

from test_hooks_cli import Env  # noqa: E402  (sets sys.path)

from swarm import cli as swarm, drive_activate as da, enrolment  # noqa: E402
from swarm.supervisor import ledger, settings as sup_settings, systemd  # noqa: E402

GOAL = "The service survives the loss of any one node."
NOW = dt.datetime(2026, 10, 10, 12, 0, tzinfo=dt.timezone.utc)
FRESH = {"last_run_at": (NOW - dt.timedelta(minutes=1)).isoformat()}
ACTIVE_TIMER = {"ActiveState": "active", "UnitFileState": "enabled"}
RECORD = re.compile(r"swarm-record: kind=([a-z_]+) id=(\d+) rev=(\d+)")


def absolute(*parts: str) -> str:
    """An absolute path on this platform (POSIX /x or Windows C:\\x), built from parts."""
    return os.path.join(os.path.abspath(os.sep), *parts)


def real_tmp() -> str:
    return os.path.realpath(tempfile.gettempdir())


class FakePerson:
    """Task 6b's swarm.person as the activation code calls it until that module is in this build: a person at
    a terminal, the requester session that owns the job, or a refusal (seat sessions first). Like the real
    require, it returns the authority or raises PermissionError."""
    tty = True
    seat_sessions = ()

    @classmethod
    def require(cls, action, job, *, board_key, env=None, confirm=None):
        from swarm import enrolment as en, hosts as ho
        sid = ho.cli_session_id(os.environ)
        if sid and sid in cls.seat_sessions:
            raise PermissionError(f"refused: {action} is a person's action; this is an enrolled agent session")
        owner = en.find_job_owner(board_key, job)
        if sid and owner is not None and owner.session_id == sid:
            return "requester"
        if cls.tty:
            return "tty"
        raise PermissionError(f"refused: {action} needs a person: run it from the session that activated {job}")


def drivegate_stub(module):
    """Stand `module` in for swarm.drivegate (None: absent). `from swarm import drivegate` reads the package
    attribute first, so once any test imported the real module (the hooks do), a sys.modules stub alone is
    shadowed and the real kick runs: replace both, whatever was imported before."""
    import contextlib
    import swarm as swarm_package
    stack = contextlib.ExitStack()
    stack.enter_context(mock.patch.dict(sys.modules, {"swarm.drivegate": module}))
    if module is None:
        if hasattr(swarm_package, "drivegate"):
            stack.enter_context(mock.patch.object(swarm_package, "drivegate", None))
            delattr(swarm_package, "drivegate")
    else:
        stack.enter_context(mock.patch.object(swarm_package, "drivegate", module, create=True))
    return stack


def ns(**kw) -> argparse.Namespace:
    base = dict(goal=None, no_drive=False, reason=None, job="J", workdir=None, allow_workdir=None,
                lead_brief=None, recipe=None, requirements=None, remote=None, credential=None,
                lead_harness=None, max_cost_usd=None, ruling_reserve_usd=None, budget=None, model=None,
                session=None, description=None)
    base.update(kw)
    return argparse.Namespace(**base)


class HomeCase(unittest.TestCase):
    """A temporary HOME (the supervisor's private directory and the ledger live under it). The suite's
    temp dir is /tmp, so the work-dir tmp check is narrowed here; the tests that exercise it set it back."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(dir=SANDBOX, prefix="drive-activate-home-"))
        env = mock.patch.dict(os.environ, home_env(self.home))
        env.start()
        self.addCleanup(env.stop)
        tmp = mock.patch.object(da, "TMP_ROOTS", ())
        tmp.start()
        self.addCleanup(tmp.stop)


class BudgetTests(HomeCase):
    def test_budget_defaults_are_the_shipped_defaults(self):
        b = da.budget_defaults({})
        self.assertEqual((b["max_cost_usd"], b["ruling_reserve_usd"], b["min_session_usd"]), (40, 5, 0.5))

    def test_config_default_used_without_flag(self):
        self.assertEqual(da.effective_budgets({"drive": {"max_cost_usd": 12}})["max_cost_usd"], 12)

    def test_max_cost_flag_overrides_config_default(self):
        overrides = da.parse_budget_args([], max_cost="20")
        self.assertEqual(da.effective_budgets({"drive": {"max_cost_usd": 12}}, overrides)["max_cost_usd"], 20)

    def test_budget_pairs_and_flags_both_apply(self):
        overrides = da.parse_budget_args(["max_sessions=7"], reserve="3")
        b = da.effective_budgets({}, overrides)
        self.assertEqual((b["max_sessions"], b["ruling_reserve_usd"]), (7, 3))

    def test_reserve_larger_than_cap_refused(self):
        with self.assertRaisesRegex(da.DriveError, "larger than max_cost_usd"):
            da.effective_budgets({}, da.parse_budget_args([], max_cost="4", reserve="5"))

    def test_cap_below_reserve_plus_min_session_refused(self):
        with self.assertRaisesRegex(da.DriveError, "below ruling_reserve_usd"):
            da.effective_budgets({}, da.parse_budget_args([], max_cost="5.2"))   # 5 + 0.5 > 5.2

    def test_cap_equal_to_reserve_plus_min_session_is_allowed(self):
        self.assertEqual(da.effective_budgets({}, da.parse_budget_args([], max_cost="5.5"))["max_cost_usd"], 5.5)

    def test_reserve_equal_to_cap_is_allowed(self):
        overrides = da.parse_budget_args(["min_session_usd=0"], max_cost="5", reserve="5")
        self.assertEqual(da.effective_budgets({}, overrides)["ruling_reserve_usd"], 5)

    def test_unknown_budget_key_refused(self):
        with self.assertRaisesRegex(da.DriveError, "KEY=VALUE"):
            da.parse_budget_args(["bogus=1"])

    def test_negative_or_non_numeric_amount_refused(self):
        for bad in ("-1", "nan", "inf", "lots", "true"):
            with self.subTest(bad=bad), self.assertRaises(da.DriveError):
                da.parse_budget_args([f"max_cost_usd={bad}"])


class ModelTests(HomeCase):
    def test_drive_models_defaults(self):
        m = da.model_defaults({})
        self.assertEqual({k: m[k] for k in ("engineer", "worker", "integrator", "reviewer", "qa", "judge",
                                            "lead", "security")},
                         {"engineer": "haiku", "worker": "haiku", "integrator": "haiku", "reviewer": "sonnet",
                          "qa": "sonnet", "judge": "opus", "lead": "opus", "security": "opus"})

    def test_job_model_override(self):
        self.assertEqual(da.effective_models({}, {"engineer": "sonnet"})["engineer"], "sonnet")

    def test_config_models_table_overrides_defaults(self):
        self.assertEqual(da.model_defaults({"drive": {"models": {"qa": "opus"}}})["qa"], "opus")

    def test_model_outside_allowed_models_refused(self):
        with self.assertRaisesRegex(da.DriveError, "allowed_models"):
            da.effective_models({}, {"engineer": "gpt-9"})

    def test_unknown_role_refused(self):
        with self.assertRaisesRegex(da.DriveError, "unknown role"):
            da.effective_models({}, {"janitor": "haiku"})

    def test_allowed_models_from_config(self):
        cfg = {"drive": {"allowed_models": ["sonnet", "opus"]}}
        with self.assertRaises(da.DriveError):
            da.model_defaults(cfg)   # the shipped engineer model (haiku) is then not allowed


class SupervisorCheckTests(HomeCase):
    def cfg(self, enabled=True):
        return {"supervise": {"enabled": enabled}}

    def test_drive_activation_refused_when_supervisor_disabled(self):
        msg = da.supervisor_problem(self.cfg(False), platform="linux", timer=ACTIVE_TIMER, state=FRESH, now=NOW)
        self.assertIn("swarm supervise enable", msg)
        self.assertIn("--no-drive --reason", msg)

    def test_drive_activation_refused_without_running_timer(self):
        msg = da.supervisor_problem(self.cfg(), platform="linux", timer={"ActiveState": "inactive"},
                                    state=FRESH, now=NOW)
        self.assertIn("not running", msg)

    def test_timer_must_be_enabled_as_well_as_active(self):
        msg = da.supervisor_problem(self.cfg(), platform="linux",
                                    timer={"ActiveState": "active", "UnitFileState": "disabled"},
                                    state=FRESH, now=NOW)
        self.assertIn("not running", msg)

    def test_drive_activation_refused_on_macos_and_windows(self):
        for plat in ("darwin", "win32"):
            with self.subTest(plat=plat):
                msg = da.supervisor_problem(self.cfg(), platform=plat, timer=ACTIVE_TIMER, state=FRESH, now=NOW)
                self.assertIn("--no-drive --reason", msg)

    def test_drive_activation_refused_without_a_recent_pass(self):
        old = {"last_run_at": (NOW - dt.timedelta(minutes=11)).isoformat()}   # 2 x 5 minutes
        self.assertIn("no supervisor pass", da.supervisor_problem(self.cfg(), platform="linux",
                                                                  timer=ACTIVE_TIMER, state=old, now=NOW))
        self.assertIn("no supervisor pass", da.supervisor_problem(self.cfg(), platform="linux",
                                                                  timer=ACTIVE_TIMER, state={}, now=NOW))

    def test_drive_activation_allowed_with_timer_and_recent_pass(self):
        self.assertIsNone(da.supervisor_problem(self.cfg(), platform="linux", timer=ACTIVE_TIMER,
                                                state=FRESH, now=NOW))


class CredentialAndWorkdirTests(HomeCase):
    def test_credential_helper_not_on_allowlist_refused(self):
        for bad in ("helper:/usr/bin/curl", "helper:gh auth git-credential", "helper:/usr/bin/gh",
                    "helper:/usr/bin/gh auth login", "helper:/usr/bin/gh auth git-credential --x"):
            with self.subTest(bad=bad), self.assertRaises(da.DriveError):
                da.credential_pin(bad)

    def test_credential_helper_on_allowlist_pinned_with_absolute_path(self):
        gh = absolute("home", "u", ".local", "bin", "gh")
        self.assertEqual(da.credential_pin("helper:" + gh + " auth git-credential"),
                         "helper:" + gh + " auth git-credential")

    def test_ssh_key_must_be_absolute(self):
        key = absolute("home", "u", ".ssh", "id")
        self.assertEqual(da.credential_pin("ssh-key:" + key), "ssh-key:" + key)
        with self.assertRaises(da.DriveError):
            da.credential_pin("ssh-key:id")

    def test_ssh_agent_pins_the_socket(self):
        sock = absolute("run", "user", "1000", "agent.sock")
        with mock.patch.dict(os.environ, {"SSH_AUTH_SOCK": sock}):
            self.assertEqual(da.credential_pin("ssh-agent"), "ssh-agent:" + sock)
        with mock.patch.dict(os.environ, {"SSH_AUTH_SOCK": ""}), self.assertRaises(da.DriveError):
            da.credential_pin("ssh-agent")

    def test_remote_urls_never_store_userinfo_query_or_fragment(self):
        self.assertEqual(da.sanitize_remote("https://user:TOKEN@example.com/o/r.git"),
                         "https://example.com/o/r.git")
        self.assertEqual(da.sanitize_remote("https://example.com/o/r.git?token=abc#frag"),
                         "https://example.com/o/r.git")
        self.assertEqual(da.sanitize_remote("git@example.com:o/r.git"), "git@example.com:o/r.git")

    def test_ssh_username_is_kept_and_its_password_is_not(self):
        self.assertEqual(da.sanitize_remote("ssh://git@example.com/o/r.git"), "ssh://git@example.com/o/r.git")
        self.assertEqual(da.sanitize_remote("ssh://git:SECRET@example.com/o/r.git?x=1#f"),
                         "ssh://git@example.com/o/r.git")
        self.assertEqual(da.sanitize_remote("https://TOKEN@example.com/o/r.git"), "https://example.com/o/r.git")
        self.assertNotIn("SECRET", da.sanitize_remote("ssh://git:SECRET@example.com:22/o/r.git"))

    def test_at_signs_in_a_password_or_query_do_not_leak_a_token(self):
        self.assertEqual(da.sanitize_remote("https://user:p@ss@example.com/o/r.git"),
                         "https://example.com/o/r.git")
        self.assertEqual(da.sanitize_remote("https://example.com/o/r.git?x=a@b"), "https://example.com/o/r.git")
        self.assertEqual(da.sanitize_remote("https://example.com?token=a@b"), "https://example.com")
        self.assertEqual(da.sanitize_remote("https://example.com/o/r.git#x@y"), "https://example.com/o/r.git")

    def test_default_credential_by_remote_kind(self):
        """[G2] the default source follows the remote's kind: a local remote (file:// or a plain path) needs
        no credential and no agent; https gets the allowlisted gh helper; ssh (ssh:// or scp-style) gets the
        ssh agent, which must be set."""
        gh = absolute("home", "u", ".local", "bin", "gh")
        sock = absolute("run", "user", "1000", "agent.sock")
        with mock.patch.dict(os.environ, {"SSH_AUTH_SOCK": ""}), \
                mock.patch.object(da.shutil, "which", return_value=gh):
            for local in ("file://" + absolute("srv", "repo.git"), absolute("srv", "repo.git")):
                with self.subTest(local=local):
                    self.assertIsNone(da.default_credential(local))
            self.assertEqual(da.default_credential("https://example.com/o/r.git"),
                             "helper:" + gh + " auth git-credential")
            for remote in ("ssh://git@example.com/o/r.git", "git@example.com:o/r.git", "example.com:o/r.git"):
                with self.subTest(ssh=remote), self.assertRaisesRegex(da.DriveError, "SSH_AUTH_SOCK"):
                    da.default_credential(remote)
        with mock.patch.dict(os.environ, {"SSH_AUTH_SOCK": sock}):
            for remote in ("ssh://git@example.com/o/r.git", "git@example.com:o/r.git", "example.com:o/r.git"):
                with self.subTest(ssh=remote):
                    self.assertEqual(da.default_credential(remote), "ssh-agent:" + sock)

    def test_local_remote_activates_without_an_ssh_agent(self):
        work = self.home / "repo"
        work.mkdir()
        with mock.patch.dict(os.environ, {"SSH_AUTH_SOCK": ""}):
            plan = da.build_plan({"supervise": {"allowed_workdirs": [str(self.home)]}},
                                 ns(job="J", workdir=str(work), remote=["origin=file:///srv/repo.git"]), GOAL)
        self.assertEqual(plan.remotes, {"origin": "file:///srv/repo.git"})
        self.assertNotIn("origin", plan.credentials)

    def test_workdir_default_refused_under_tmp(self):
        tmp = real_tmp()
        cfg = {"supervise": {"allowed_workdirs": [tmp]}}
        with mock.patch.object(da, "TMP_ROOTS", (tmp,)), \
                self.assertRaisesRegex(da.DriveError, "never /tmp"):
            da.resolve_workdir(cfg, None, os.path.join(tmp, "some-job"))

    def test_workdir_outside_allowed_workdirs_refused(self):
        cfg = {"supervise": {"allowed_workdirs": [str(self.home / "src")]}}
        with self.assertRaisesRegex(da.DriveError, "allowed_workdirs"):
            da.resolve_workdir(cfg, None, str(self.home / "elsewhere"))

    def test_sibling_directory_sharing_a_prefix_is_not_under_the_root(self):
        cfg = {"supervise": {"allowed_workdirs": [str(self.home / "src")]}}
        sibling = self.home / "src-other"
        sibling.mkdir(parents=True)
        with self.assertRaisesRegex(da.DriveError, "allowed_workdirs"):
            da.resolve_workdir(cfg, str(sibling), str(self.home))

    def test_workdir_under_allowed_root_accepted(self):
        cfg = {"supervise": {"allowed_workdirs": [str(self.home / "src")]}}
        work = self.home / "src" / "repo"
        work.mkdir(parents=True)
        self.assertEqual(da.resolve_workdir(cfg, None, str(work)), os.path.realpath(work))

    def test_generated_brief_is_git_excluded(self):
        work = self.home / "repo"
        (work / ".git" / "info").mkdir(parents=True)
        path = da.write_lead_brief(str(work), "J", "# brief\n")
        self.assertEqual(path, work / ".swarm" / "lead-brief-J.md")
        self.assertIn(".swarm/", (work / ".git" / "info" / "exclude").read_text().splitlines())
        da.write_lead_brief(str(work), "J", "# brief\n")
        self.assertEqual((work / ".git" / "info" / "exclude").read_text().splitlines().count(".swarm/"), 1)

    def test_blob_id_is_the_git_blob_id(self):
        self.assertEqual(da.blob_id(b"hello\n"), "ce013625030ba8dba906f756967f9e9ca394464a")


class PinnedGitTests(HomeCase):
    """[G2] every git call that takes a pinned URL: the URL is never an option (`--` before it, and a URL
    starting with "-" is refused everywhere it enters), the transports are pinned (https, ssh, file), and
    git runs in a neutral directory with no system or global config, so config a seat wrote (a work dir's
    .git/config, ~/.gitconfig) cannot rewrite the URL or enable a protocol."""

    def git(self, *args, cwd=None):
        import subprocess
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
        return subprocess.run(["git", *args], check=True, capture_output=True, text=True, env=env, cwd=cwd)

    def upstream(self, branch="main"):
        repo = self.home / "upstream"
        self.git("init", "-q", "-b", branch, str(repo))
        self.git("-C", str(repo), "commit", "-q", "--allow-empty", "-m", "one")
        return repo, self.git("-C", str(repo), "rev-parse", "HEAD").stdout.strip()

    def test_upload_pack_url_does_not_execute(self):
        """The reviewer's PoC: `git ls-remote --heads --upload-pack=CMD ...` runs CMD. It is refused before
        git runs, and even if it reached git the `--` makes it a repository name."""
        marker = self.home / "pwned-upload-pack"
        url = "--upload-pack=touch${IFS}" + str(marker)
        with self.assertRaises(da.DriveError):
            da.remote_head(url, None, None, "main")
        self.assertFalse(marker.exists())

    def test_upload_pack_url_is_inert_even_past_the_validation(self):
        marker = self.home / "pwned-upload-pack-2"
        url = "--upload-pack=touch${IFS}" + str(marker)
        with mock.patch.object(da.gitpin, "check_url", side_effect=lambda u: u):
            with self.assertRaises(da.DriveError):
                da.remote_head(url, None, None, "main")
        self.assertFalse(marker.exists())

    def test_argv_has_double_dash_before_the_url_and_env_pins(self):
        done = types.SimpleNamespace(returncode=0, stdout="", stderr="")
        url = "https://example.com/o/r.git"
        with mock.patch.object(da.subprocess, "run", return_value=done) as run:
            da.remote_head(url, None, None, "main")
        argv, kw = run.call_args.args[0], run.call_args.kwargs
        self.assertEqual(argv[argv.index(url) - 1], "--")
        self.assertLess(argv.index("ls-remote"), argv.index("--"))
        env = kw["env"]
        self.assertEqual(env["GIT_ALLOW_PROTOCOL"], "https:ssh:file")
        self.assertEqual(env["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertEqual(env["GIT_CONFIG_GLOBAL"], os.devnull)
        self.assertIn("protocol.allow=never", argv)
        self.assertEqual(kw["cwd"], os.path.abspath(os.sep))
        self.assertNotEqual(kw["cwd"], os.getcwd())

    def test_planted_insteadof_in_the_workdir_config_does_not_run(self):
        """The reviewer's second PoC: the work dir's .git/config (seat-writable) rewrites the URL to ext::
        and allows ext. With the process inside that work dir, the read still goes to the pinned URL."""
        repo, head = self.upstream()
        work = self.home / "work"
        self.git("init", "-q", str(work))
        marker = self.home / "pwned-insteadof"
        url = "file://" + str(repo)
        self.git("-C", str(work), "config", "protocol.ext.allow", "always")
        self.git("-C", str(work), "config", f"url.ext::sh -c touch% {marker}% >&2.insteadOf", url)
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)
        os.chdir(work)
        self.assertEqual(da.remote_head(url, None, None, "main"), head)
        self.assertFalse(marker.exists())

    def test_planted_global_config_does_not_redirect_the_url(self):
        repo, head = self.upstream()
        decoy = self.home / "decoy"
        self.git("init", "-q", "-b", "main", str(decoy))
        self.git("-C", str(decoy), "commit", "-q", "--allow-empty", "-m", "decoy")
        url = "file://" + str(repo)
        (self.home / ".gitconfig").write_text(f'[url "file://{decoy}"]\n\tinsteadOf = {url}\n')
        self.assertEqual(da.remote_head(url, None, None, "main"), head)

    def test_ext_and_unpinned_transports_refused(self):
        for bad in ("ext::sh -c touch% x", "ext::sh", "fd::3", "git://example.com/r.git", "http://example.com/r.git",
                    "ftp://example.com/r.git", "-oProxyCommand=x", "ssh://-oProxyCommand=x/r", "relative/path",
                    "https://example.com/r\n.git", "https://exa mple.com/r.git"):
            with self.subTest(url=bad), self.assertRaises(da.DriveError):
                da.remote_head(bad, None, None, "main")

    def test_sanitize_remote_refuses_an_option_or_a_transport_helper(self):
        for bad in ("--upload-pack=touch${IFS}x", "-u", "ext::sh -c id", "git://example.com/r.git",
                    "https://example.com/r.git\n", "relative/repo"):
            with self.subTest(url=bad), self.assertRaises(da.DriveError):
                da.sanitize_remote(bad)

    def test_remote_flag_with_an_option_url_is_refused_at_plan_time(self):
        work = self.home / "repo"
        work.mkdir()
        with self.assertRaises(da.DriveError):
            da.build_plan({"supervise": {"allowed_workdirs": [str(self.home)]}},
                          ns(job="J", workdir=str(work), remote=["origin=--upload-pack=touch${IFS}x"]), GOAL)

    def test_workdir_remote_with_an_option_url_is_never_pinned(self):
        work = self.home / "w2"
        self.git("init", "-q", str(work))
        self.git("-C", str(work), "config", "remote.evil.url", "--upload-pack=touch${IFS}x")
        self.git("-C", str(work), "config", "remote.rel.url", "../up.git")
        remotes = da.git_remotes(str(work))
        self.assertNotIn("evil", remotes)
        self.assertEqual(remotes.get("rel"), str(self.home / "up.git"))

    def test_pin_read_back_is_rechecked_against_the_allowlist(self):
        done = types.SimpleNamespace(returncode=0, stdout="", stderr="")
        for pin in ("helper:/usr/bin/curl", "helper:/usr/bin/gh auth login", "helper:gh auth git-credential",
                    "ssh-key:relative", "ssh-agent:relative", "nonsense"):
            with self.subTest(pin=pin), mock.patch.object(da.subprocess, "run", return_value=done) as run:
                with self.assertRaises(da.DriveError):
                    da.remote_head("https://example.com/r.git", pin, None, "main")
                run.assert_not_called()


class ConfigDirTests(HomeCase):
    """[G2a] a gh helper's GH_CONFIG_DIR must be private: a seat that can write it can plant hosts.yml, and
    the pushes then use the seat's token."""

    def setUp(self):
        super().setUp()
        self.root = self.home / "src"
        self.root.mkdir(mode=0o700)
        self.cfg = {"supervise": {"allowed_workdirs": [str(self.root)]}}

    def private(self, name="gh"):
        d = self.home / name
        d.mkdir(mode=0o700)
        os.chmod(d, 0o700)
        return str(d)

    def test_private_dir_accepted(self):
        d = self.private()
        self.assertEqual(da.check_config_dir(d, self.cfg), d)

    def test_dir_under_an_allowed_workdirs_root_refused(self):
        d = self.root / "repo" / ".gh"
        d.mkdir(parents=True, mode=0o700)
        with self.assertRaisesRegex(da.DriveError, "work root"):
            da.check_config_dir(str(d), self.cfg)

    def test_dir_under_a_job_root_refused(self):
        d = self.private("extra")
        with self.assertRaisesRegex(da.DriveError, "work root"):
            da.check_config_dir(d, self.cfg, [d])

    def test_missing_dir_refused(self):
        with self.assertRaisesRegex(da.DriveError, "does not exist"):
            da.check_config_dir(str(self.home / "nope"), self.cfg)

    @posix_only("needs POSIX file modes and uids")
    def test_group_or_world_writable_dir_refused(self):
        for mode in (0o770, 0o707):
            with self.subTest(mode=oct(mode)):
                d = self.private(f"gh-{mode:o}")
                os.chmod(d, mode)
                with self.assertRaisesRegex(da.DriveError, "group or others"):
                    da.check_config_dir(d, self.cfg)

    @posix_only("needs POSIX file modes and uids")
    def test_writable_parent_refused(self):
        parent = self.home / "shared"
        parent.mkdir()
        os.chmod(parent, 0o777)
        (parent / "gh").mkdir(mode=0o700)
        with self.assertRaisesRegex(da.DriveError, "parent"):
            da.check_config_dir(str(parent / "gh"), self.cfg)

    @posix_only("needs POSIX file modes and uids")
    def test_sticky_parent_allowed(self):
        parent = self.home / "sticky"
        parent.mkdir()
        os.chmod(parent, 0o1777)
        (parent / "gh").mkdir(mode=0o700)
        self.assertEqual(da.check_config_dir(str(parent / "gh"), self.cfg), str(parent / "gh"))

    @posix_only("needs POSIX file modes and uids")
    def test_dir_of_another_user_refused(self):
        d = self.private()
        real = os.stat(d)
        fake = os.stat_result((real.st_mode, real.st_ino, real.st_dev, real.st_nlink, os.getuid() + 1,
                               real.st_gid, real.st_size, int(real.st_atime), int(real.st_mtime), int(real.st_ctime)))
        orig = os.stat
        with mock.patch.object(da.gitpin.os, "stat", side_effect=lambda p, *a, **k: fake if p == os.path.realpath(d) else orig(p, *a, **k)):
            with self.assertRaisesRegex(da.DriveError, "not owned"):
                da.check_config_dir(d, self.cfg)

    def test_plan_refuses_a_gh_config_dir_under_the_workdir(self):
        work = self.root / "repo"
        work.mkdir()
        planted = work / ".config" / "gh"
        planted.mkdir(parents=True, mode=0o700)
        gh = absolute("home", "u", ".local", "bin", "gh")
        with mock.patch.dict(os.environ, {"GH_CONFIG_DIR": str(planted)}), \
                self.assertRaisesRegex(da.DriveError, "work root"):
            da.build_plan(self.cfg, ns(job="J", workdir=str(work), remote=["origin=https://example.com/o/r.git"],
                                       credential=["origin=helper:" + gh + " auth git-credential"]), GOAL)

    def test_config_dir_rechecked_when_read_back_from_the_record(self):
        """A record whose config dir became unsafe (or was planted) refuses before git runs."""
        work = self.root / "repo"
        work.mkdir()
        bad = work / "gh"
        bad.mkdir(mode=0o700)
        o = {"target_branch": "main", "remotes": {"origin": "https://example.com/r.git"},
             "credentials": {"origin": "helper:/usr/bin/gh auth git-credential"},
             "config_dirs": {"origin": str(bad)}, "roots": [str(work)]}
        led = types.SimpleNamespace(activation_heads={})
        with mock.patch.object(ledger, "load", return_value=led), \
                mock.patch.object(da, "remote_head") as head, self.assertRaisesRegex(da.DriveError, "work root"):
            da._record_activation_heads(self.cfg, "bk", "J", o)
        head.assert_not_called()

    def test_pin_rechecked_when_read_back_from_the_record(self):
        o = {"target_branch": "main", "remotes": {"origin": "https://example.com/r.git"},
             "credentials": {"origin": "helper:/usr/bin/curl"}, "config_dirs": {}, "roots": [str(self.root)]}
        led = types.SimpleNamespace(activation_heads={})
        with mock.patch.object(ledger, "load", return_value=led), \
                mock.patch.object(da, "remote_head") as head, self.assertRaisesRegex(da.DriveError, "allowlist"):
            da._record_activation_heads(self.cfg, "bk", "J", o)
        head.assert_not_called()


class OwnerRecordTests(HomeCase):
    """The owner record validator refuses what a pinned git call refuses, and an owner record this build
    cannot read is a clear DRIVER-CONFIG hold, not a silent "no record" (a misleading DRIVER-ORPHAN)."""

    def owner(self, **kw):
        base = dict(roots=[absolute("w")], remotes={"origin": "https://example.com/r.git"},
                    credentials={"origin": "helper:/usr/bin/gh auth git-credential"}, requirements={},
                    recipe="coding", goal=GOAL, brief=absolute("w", "b.md"), target_branch="main",
                    config_dirs={"origin": absolute("home", "u", ".config", "gh")})
        base.update(kw)
        return base

    def write(self, **kw):
        return enrolment.write_job_owner("bk", job="J", harness="claude", session_id=None, cwd=absolute("w"),
                                         **self.owner(**kw))

    def test_valid_owner_record_round_trips_with_its_format(self):
        self.write()
        rec = enrolment.find_job_owner("bk", "J", strict=True)
        self.assertEqual(rec.owner["format"], enrolment.OWNER_FORMAT)

    def test_option_or_helper_urls_refused_in_the_owner_record(self):
        for url in ("--upload-pack=touch${IFS}x", "-x", "ext::sh -c id", "git://example.com/r.git", "rel/path"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.write(remotes={"origin": url})

    def test_pins_off_the_allowlist_refused_in_the_owner_record(self):
        for pin in ("helper:/usr/bin/curl", "helper:gh auth git-credential", "ssh-key:rel", "x"):
            with self.subTest(pin=pin), self.assertRaises(ValueError):
                self.write(credentials={"origin": pin})

    def test_target_branch_regex_applied_in_the_owner_record(self):
        for branch in ("-x", "a..b", "a b", "a\nb", "/x", "x.lock", "x/"):
            with self.subTest(branch=branch), self.assertRaises(ValueError):
                self.write(target_branch=branch)

    def planted(self, owner):
        """Write an owner record file directly (as an older build, or a planter, would)."""
        import json as _json
        rec = {"v": enrolment.FORMAT, "kind": "owner", "board_key": "bk", "job": "J", "agent_key": None,
               "harness": "claude", "session_id": None, "cwd": absolute("w"), "created_at": 1.0, "prompt": None,
               "owner": owner}
        with enrolment._dir(create=True) as d:
            enrolment.safefs.write_atomic(d, f"owner-{enrolment._digest('owner', 'bk', 'J')}.json", _json.dumps(rec))

    def test_old_format_owner_record_is_a_clear_error_not_a_missing_record(self):
        old = self.owner()
        old.pop("target_branch"), old.pop("config_dirs")          # a format-1 drive record
        self.planted(old)
        self.assertIsNone(enrolment.find_job_owner("bk", "J"))
        with self.assertRaisesRegex(enrolment.OwnerRecordInvalid, "owner record format/fields invalid"):
            enrolment.find_job_owner("bk", "J", strict=True)
        with mock.patch.object(da, "board_key", return_value="bk"), \
                self.assertRaisesRegex(da.DriveError, "DRIVER-CONFIG.*owner record format/fields invalid"):
            da._owner("bk", "J")

    def test_unknown_field_or_planted_url_in_a_current_record_is_a_clear_error(self):
        for owner in ({**self.owner(), "format": 2, "extra": 1},
                      {**self.owner(), "format": 2, "remotes": {"o": "--upload-pack=x"}}):
            self.planted(owner)
            with self.subTest(owner=sorted(owner)), self.assertRaisesRegex(enrolment.OwnerRecordInvalid,
                                                                           "format/fields invalid"):
                enrolment.find_job_owner("bk", "J", strict=True)

    def test_no_record_is_none_when_strict(self):
        self.assertIsNone(enrolment.find_job_owner("bk", "J", strict=True))

    def test_prune_keeps_an_owner_record_it_cannot_read(self):
        old = self.owner()
        old.pop("target_branch")
        self.planted(old)
        enrolment.prune(0.0, now=10.0 ** 9)
        with self.assertRaises(enrolment.OwnerRecordInvalid):
            enrolment.find_job_owner("bk", "J", strict=True)


class KickTests(unittest.TestCase):
    def test_absent_drivegate_is_silent(self):
        import io
        from contextlib import redirect_stderr
        err = io.StringIO()
        with drivegate_stub(None), redirect_stderr(err):
            self.assertFalse(da.kick())
        self.assertEqual(err.getvalue(), "")

    def test_failing_kick_is_reported_loudly_and_not_raised(self):
        import io
        from contextlib import redirect_stderr
        kicker = types.SimpleNamespace(kick=mock.Mock(side_effect=RuntimeError("socket gone")))
        err = io.StringIO()
        with drivegate_stub(kicker), redirect_stderr(err):
            self.assertFalse(da.kick())
        self.assertIn("WARNING: the supervisor kick failed (RuntimeError: socket gone)", err.getvalue())

    def test_broken_drivegate_import_is_reported(self):
        import io
        import builtins
        from contextlib import redirect_stderr
        real = builtins.__import__

        def fake(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "swarm" and fromlist and "drivegate" in fromlist:
                raise ModuleNotFoundError("No module named 'yaml'", name="yaml")
            return real(name, globals, locals, fromlist, level)
        err = io.StringIO()
        with mock.patch.object(builtins, "__import__", side_effect=fake), redirect_stderr(err):
            self.assertFalse(da.kick())
        self.assertIn("kick failed (ModuleNotFoundError", err.getvalue())


class FlagTests(HomeCase):
    def parser(self):
        p = argparse.ArgumentParser()
        da.add_activate_args(p)
        return p

    def test_goal_implies_drive(self):
        self.assertTrue(da.wants_drive(ns(goal="g")))
        self.assertFalse(da.wants_drive(ns(goal="g", no_drive=True)))
        self.assertFalse(da.wants_drive(ns(goal=None)))

    def test_no_drive_requires_reason(self):
        self.assertEqual(da.legacy_goal_check(ns(goal="g", no_drive=True, reason=None)), 2)
        self.assertEqual(da.legacy_goal_check(ns(goal="g", no_drive=True, reason="  ")), 2)
        self.assertIsNone(da.legacy_goal_check(ns(goal="g", no_drive=True, reason="a person drives it")))

    def test_drive_flag_is_accepted_noop(self):
        args = self.parser().parse_args(["--drive"])
        self.assertTrue(args.drive)



class DriveCli(Env):
    """`swarm activate --goal` through the CLI, on the test board. The systemd timer and the supervisor's
    last pass are stubbed; `systemctl` and the timer install are never run (they would change the
    real user's timer)."""

    def setUp(self):
        super().setUp()
        self.work = self.tmp / "src" / "repo"
        self.work.mkdir(parents=True)
        self.base_toml = self.config.read_text()
        self.enable_supervisor(True)
        tmp = mock.patch.object(da, "TMP_ROOTS", ())   # the suite's temp dir is the temp dir; see HomeCase
        tmp.start()
        self.addCleanup(tmp.stop)
        FakePerson.tty, FakePerson.seat_sessions = True, ()
        # `from swarm import person` reads the package attribute first, so an earlier import of the real
        # module would shadow a sys.modules stub: replace both, whatever was imported before this test
        person = mock.patch.dict(sys.modules, {"swarm.person": FakePerson})
        person.start()
        self.addCleanup(person.stop)
        import swarm as swarm_package   # the package: its `person` attribute is what `from swarm import person` reads
        attr = mock.patch.object(swarm_package, "person", FakePerson, create=True)
        attr.start()
        self.addCleanup(attr.stop)
        # the real kick (Task 5's drivegate) would start the user's supervisor unit: a stub kicks here
        stub = drivegate_stub(types.SimpleNamespace(kick=mock.Mock(return_value=True)))
        stub.__enter__()
        self.addCleanup(stub.__exit__, None, None, None)
        self.platform = mock.patch.object(da, "PLATFORM", "linux")   # the timer check is for Linux here
        self.platform.start()
        self.addCleanup(self.platform.stop)
        self.timer = mock.patch.object(da, "timer_state", return_value=dict(ACTIVE_TIMER))
        self.timer.start()
        self.addCleanup(self.timer.stop)
        self.fresh_pass()
        step = types.SimpleNamespace(name="supervisor", status="ok", detail="swarm-supervise.timer: every 5 min")
        for target in (mock.patch.object(systemd, "install", return_value=step),
                       mock.patch("swarm.supervisor.switch.stop_timer", return_value="stopped")):
            target.start()
            self.addCleanup(target.stop)

    def fresh_pass(self):
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        p = mock.patch.object(da, "last_pass_state", return_value={"last_run_at": now})
        p.start()
        self.addCleanup(p.stop)

    def enable_supervisor(self, enabled: bool):
        self.config.write_text(self.base_toml + f"\n[supervise]\nenabled = {str(enabled).lower()}\n"
                               f"allowed_workdirs = [{json_str(str(self.tmp))}]\n")
        self.cfg = swarm.load_config(self.config)

    def activate(self, *extra, job="J"):
        return self.cli("activate", "--job", job, "--goal", GOAL, *extra)

    def ledger_of(self, job="J"):
        return ledger.load(da.board_key(self.cfg), job)

    def plan(self, job="J", **kw):
        return da.build_plan(self.cfg, ns(job=job, workdir=str(self.work), session="sess-1", **kw), GOAL)

    def test_activate_drive_creates_ledger_and_lead_seat(self):
        rc, out, err = self.activate("--drive", "--workdir", str(self.work))
        self.assertEqual(rc, 0, err)
        self.assertIsNotNone(self.ledger_of())
        self.assertEqual(self.ledger_of().state, "active")
        with self.board() as b:
            lead = b.seat("J", 1, "lead")
        self.assertEqual((lead.role, lead.model, lead.state), ("engineering_lead", "opus", "requested"))
        last = out.strip().splitlines()[-1]
        self.assertRegex(last, RECORD)
        self.assertEqual(RECORD.fullmatch(last).group(1), "activate")

    def test_goal_implies_drive_and_pins_the_owner_details(self):
        rc, _, err = self.activate("--workdir", str(self.work))
        self.assertEqual(rc, 0, err)
        owner = enrolment.find_job_owner(da.board_key(self.cfg), "J")
        self.assertEqual(owner.owner["roots"], [os.path.realpath(self.work)])
        self.assertEqual(owner.owner["goal"], GOAL)
        self.assertEqual(owner.owner["requirements"]["goal"], da.blob_id(GOAL.encode()))

    def test_owner_record_carries_the_recipe_review_keys(self):
        rc, _, err = self.activate("--workdir", str(self.work))
        self.assertEqual(rc, 0, err)
        owner = enrolment.find_job_owner(da.board_key(self.cfg), "J").owner
        self.assertEqual((owner["recipe"], owner["review_role"], owner["review_gates"]),
                         ("coding", "reviewer", "haiku-strict"))   # the engineer defaults to haiku

    def test_plan_decides_the_review_keys_from_the_recipe(self):
        plan = self.plan()
        self.assertEqual((plan.review_role, plan.review_gates), ("reviewer", "haiku-strict"))
        artifact = self.plan(recipe="artifact")
        self.assertEqual((artifact.review_role, artifact.review_gates), (None, None))

    def test_review_gates_standard_when_the_engineer_is_not_haiku(self):
        rc, _, err = self.activate("--workdir", str(self.work), "--model", "engineer=sonnet")
        self.assertEqual(rc, 0, err)
        self.assertEqual(enrolment.find_job_owner(da.board_key(self.cfg), "J").owner["review_gates"], "standard")

    def test_artifact_recipe_has_no_review_stage(self):
        rc, _, err = self.activate("--workdir", str(self.work), "--recipe", "artifact")
        self.assertEqual(rc, 0, err)
        owner = enrolment.find_job_owner(da.board_key(self.cfg), "J").owner
        self.assertEqual((owner["recipe"], owner["review_role"], owner["review_gates"]), ("artifact", None, None))
        self.assertFalse(owner["review_role"])   # the driver's reviewer check: falsy means no reviewer launches

    def test_activation_sets_drive_enabled_on_the_board(self):
        rc, _, err = self.activate("--workdir", str(self.work))
        self.assertEqual(rc, 0, err)
        with self.board() as b:
            self.assertEqual(b.job_data("J").get("drive.enabled"), "1")

    def test_drive_flag_is_accepted_noop(self):
        rc, _, err = self.activate("--drive", "--workdir", str(self.work))
        self.assertEqual(rc, 0, err)

    def test_drive_activation_refused_when_supervisor_disabled(self):
        self.enable_supervisor(False)
        rc, _, err = self.activate("--workdir", str(self.work))
        self.assertEqual(rc, 1)
        self.assertIn("swarm supervise enable", err)
        self.assertIn("--no-drive --reason", err)
        self.assertIsNone(self.ledger_of())

    def test_drive_activation_refused_without_running_timer(self):
        self.timer.stop()
        with mock.patch.object(da, "timer_state", return_value={"ActiveState": "inactive"}):
            rc, _, err = self.activate("--workdir", str(self.work))
        self.assertEqual(rc, 1)
        self.assertIn("not running", err)

    def test_no_drive_requires_reason(self):
        rc, _, err = self.activate("--no-drive", "--workdir", str(self.work))
        self.assertEqual(rc, 2)
        self.assertIn("--reason", err)

    def test_no_drive_with_reason_is_shown_not_autonomous(self):
        self.enable_supervisor(False)
        rc, out, err = self.activate("--no-drive", "--reason", "a person drives it")
        self.assertEqual(rc, 0, err)
        self.assertIn("not driven by the supervisor", out)
        self.assertIsNone(self.ledger_of())

    def test_workdir_default_refused_under_tmp(self):
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)
        with mock.patch.object(da, "TMP_ROOTS", (real_tmp(),)):
            os.chdir(self.tmp)
            rc, _, err = self.activate()
        self.assertEqual(rc, 2)
        self.assertIn("never /tmp", err)

    def test_default_workdir_is_the_current_directory(self):
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)
        os.chdir(self.work)
        rc, _, err = self.activate()
        self.assertEqual(rc, 0, err)

    def test_max_cost_flag_overrides_config_default(self):
        rc, _, err = self.activate("--workdir", str(self.work), "--max-cost-usd", "20")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.ledger_of().budgets["max_cost_usd"], 20)

    def test_config_default_used_without_flag(self):
        self.config.write_text(self.config.read_text() + "\n[drive]\nmax_cost_usd = 12\n")
        self.cfg = swarm.load_config(self.config)
        rc, _, err = self.activate("--workdir", str(self.work))
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.ledger_of().budgets["max_cost_usd"], 12)

    def test_reserve_larger_than_cap_refused(self):
        rc, _, err = self.activate("--workdir", str(self.work), "--max-cost-usd", "4", "--ruling-reserve-usd", "5")
        self.assertEqual(rc, 2)
        self.assertIn("larger than", err)
        self.assertIsNone(self.ledger_of())

    def test_model_override_per_job(self):
        rc, _, err = self.activate("--workdir", str(self.work), "--model", "engineer=sonnet")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.ledger_of().models, {"engineer": "sonnet"})

    def test_credential_helper_not_on_allowlist_refused(self):
        curl = absolute("usr", "bin", "curl")
        rc, _, err = self.activate("--workdir", str(self.work), "--remote", "origin=https://example.com/r.git",
                                   "--credential", "origin=helper:" + curl)
        self.assertEqual(rc, 2)
        self.assertIn("allowlist", err)
        self.assertIsNone(self.ledger_of())

    def make_repo(self, branch="main", name="upstream"):
        """A real git repository with one commit on `branch`: the pinned-remote tests read it with git."""
        import subprocess
        repo = self.tmp / name
        repo.mkdir()
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
        subprocess.run(["git", "init", "-q", "-b", branch, str(repo)], check=True, env=env)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", "one"], check=True, env=env)
        head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True,
                              text=True, env=env).stdout.strip()
        return repo, head, env

    def test_activation_records_the_target_head_per_remote(self):
        """[11b] the target's head on each pinned remote is recorded at activation: the audit's `since`."""
        repo, head, _ = self.make_repo()
        url = "file://" + str(repo)
        rc, _, err = self.activate("--workdir", str(self.work), "--remote", "origin=" + url)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.ledger_of().activation_heads, {url: head})

    def test_activation_head_survives_a_rollover_unchanged(self):
        import subprocess
        repo, head, env = self.make_repo()
        url = "file://" + str(repo)
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "sess-1"}):
            self.assertEqual(self.activate("--workdir", str(self.work), "--remote", "origin=" + url)[0], 0)
            subprocess.run(["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", "two"], check=True, env=env)
            rc, _, err = self.activate("--workdir", str(self.work), "--remote", "origin=" + url)
        self.assertEqual(rc, 0, err)
        led = self.ledger_of()
        self.assertEqual((led.generation_requested, led.activation_heads), (2, {url: head}))

    def test_missing_target_branch_refuses_activation_and_holds_it_activating(self):
        repo, _, _ = self.make_repo(branch="dev")
        rc, _, err = self.activate("--workdir", str(self.work), "--remote", "origin=file://" + str(repo))
        self.assertEqual(rc, 1)
        self.assertIn("not on the remote", err)
        self.assertEqual(self.ledger_of().state, "activating")

    def test_target_branch_is_pinned_from_the_pipeline_config(self):
        self.config.write_text(self.config.read_text() + '\n[pipeline]\ntarget_branch = "release"\n')
        self.cfg = swarm.load_config(self.config)
        rc, _, err = self.activate("--workdir", str(self.work))
        self.assertEqual(rc, 0, err)
        self.assertEqual(enrolment.find_job_owner(da.board_key(self.cfg), "J").owner["target_branch"], "release")

    def test_target_branch_defaults_to_main_and_accepts_the_merge_target_alias(self):
        self.assertEqual(da.pipeline_target({}), "main")
        self.assertEqual(da.pipeline_target({"pipeline": {"merge_target": "dev"}}), "dev")
        self.assertEqual(da.pipeline_target({"pipeline": {"target_branch": "rel", "merge_target": "dev"}}), "rel")
        with self.assertRaises(da.DriveError):
            da.pipeline_target({"pipeline": {"target_branch": "a..b"}})

    def test_gh_helper_pins_its_config_dir_per_destination(self):
        gh = absolute("home", "u", ".local", "bin", "gh")
        cfgdir = tempfile.mkdtemp(dir=SANDBOX, prefix="gh-config-")   # private, outside every work root
        self.addCleanup(os.rmdir, cfgdir)
        with mock.patch.dict(os.environ, {"GH_CONFIG_DIR": cfgdir}), \
                mock.patch.object(da, "remote_head", return_value="a" * 40):
            rc, _, err = self.activate("--workdir", str(self.work), "--remote",
                                       "origin=https://example.com/o/r.git",
                                       "--credential", "origin=helper:" + gh + " auth git-credential")
        self.assertEqual(rc, 0, err)
        self.assertEqual(enrolment.find_job_owner(da.board_key(self.cfg), "J").owner["config_dirs"],
                         {"origin": cfgdir})

    def test_non_gh_helper_pins_no_config_dir(self):
        helper = absolute("usr", "lib", "git-credential-store")
        with mock.patch.object(da, "remote_head", return_value="a" * 40):
            rc, _, err = self.activate("--workdir", str(self.work), "--remote",
                                       "origin=https://example.com/o/r.git",
                                       "--credential", "origin=helper:" + helper)
        self.assertEqual(rc, 0, err)
        self.assertEqual(enrolment.find_job_owner(da.board_key(self.cfg), "J").owner["config_dirs"], {})

    def test_undeterminable_gh_config_dir_is_refused_not_guessed(self):
        gh = absolute("home", "u", ".local", "bin", "gh")
        orig = os.path.expanduser
        with mock.patch.dict(os.environ, {"GH_CONFIG_DIR": "", "XDG_CONFIG_HOME": ""}), \
                mock.patch.object(da.os.path, "expanduser", side_effect=lambda p: "~" if p == "~" else orig(p)), \
                mock.patch.object(da, "remote_head", return_value="a" * 40):
            rc, _, err = self.activate("--workdir", str(self.work), "--remote",
                                       "origin=https://example.com/o/r.git",
                                       "--credential", "origin=helper:" + gh + " auth git-credential")
        self.assertEqual(rc, 2)
        self.assertIn("cannot be determined", err)
        self.assertIsNone(self.ledger_of())

    def test_gh_config_dir_resolution_order(self):
        self.assertEqual(da.gh_config_dir({"GH_CONFIG_DIR": "/x/one", "XDG_CONFIG_HOME": "/x/two"}), "/x/one")
        self.assertEqual(da.gh_config_dir({"XDG_CONFIG_HOME": "/x/two"}), os.path.join("/x/two", "gh"))
        with self.assertRaises(da.DriveError):
            da.gh_config_dir({"GH_CONFIG_DIR": "relative/gh"})

    def test_the_helper_is_given_the_pinned_config_dir(self):
        gh = absolute("home", "u", ".local", "bin", "gh")
        cfgdir = absolute("home", "u", "gh-config")
        done = types.SimpleNamespace(returncode=0, stdout="a" * 40 + "\trefs/heads/main\n", stderr="")
        with mock.patch.object(da.subprocess, "run", return_value=done) as run:
            sha = da.remote_head("https://example.com/o/r.git", "helper:" + gh + " auth git-credential",
                                 cfgdir, "main")
        self.assertEqual(sha, "a" * 40)
        env, argv = run.call_args.kwargs["env"], run.call_args.args[0]
        self.assertEqual(env["GH_CONFIG_DIR"], cfgdir)
        self.assertIn("credential.helper=!" + gh + " auth git-credential", argv)

    def test_activating_ledger_completed_by_next_pass(self):
        """A crash after each durable step: the next pass (finish_activation) completes the job, and
        nothing is duplicated. A crash before the ledger exists leaves nothing to complete; the activation
        is then run again and finishes. Each case has its own job name."""
        points = ("brief", "owner", "claim", "ledger", "data", "heads", "lead")
        for i, point in enumerate(points):
            job = f"J{i}"
            with self.subTest(crash=point):
                plan = self.plan(job=job)
                with self.board() as b:
                    with self.assertRaises(RuntimeError):
                        if point in ("brief", "owner", "claim", "ledger"):
                            da.activate_drive(self.cfg, b, plan, crash_after=point)
                        else:
                            da.activate_drive(self.cfg, b, plan)
                            da.finish_activation(self.cfg, b, job, crash_after=point)
                    # the window rule: a ledger never exists without its board claim
                    if self.ledger_of(job) is not None:
                        self.assertEqual(b.job_data(job).get("drive.enabled"), "1")
                    else:
                        da.activate_drive(self.cfg, b, plan)          # the person runs it again
                    da.finish_activation(self.cfg, b, job)           # the next pass
                    da.finish_activation(self.cfg, b, job)           # and again: a no-op
                    self.assertEqual(self.ledger_of(job).state, "active")
                    self.assertEqual(len(b.seats(job, 1)), 1)
                    self.assertEqual(b.job_status(job).status, "active")
                    self.assertEqual(b.job_data(job).get("drive.enabled"), "1")

    def test_finish_never_writes_the_claim_after_the_ledger(self):
        """[RR1a]: a ledger whose claim is missing is held, not completed with a claim written after it."""
        plan = self.plan(job="JC")
        with self.board() as b:
            with self.assertRaises(RuntimeError):
                da.activate_drive(self.cfg, b, plan, crash_after="ledger")
            b.set_job_data("JC", "drive.enabled", None)          # the claim is lost
            with self.assertRaisesRegex(da.DriveError, "DRIVER-ORPHAN"):
                da.finish_activation(self.cfg, b, "JC")
            self.assertIsNone(b.job_data("JC").get("drive.enabled"))
            self.assertEqual(self.ledger_of("JC").state, "activating")

    def test_claim_is_written_before_the_ledger_and_never_after(self):
        """[RR1a]: the activation's first claim write comes before the ledger, and no later step writes it."""
        events = []
        real_create = ledger.create

        def create(*a, **kw):
            events.append("ledger")
            return real_create(*a, **kw)
        with self.board() as b:
            real_set = b.set_job_data

            def set_job_data(job, key, *a, **kw):
                if key == "drive.enabled":
                    events.append("claim")
                return real_set(job, key, *a, **kw)
            b.set_job_data = set_job_data
            with mock.patch.object(ledger, "create", side_effect=create):
                da.activate_drive(self.cfg, b, self.plan(job="JO"))
                da.finish_activation(self.cfg, b, "JO")
        self.assertEqual(events, ["claim", "ledger"])

    def test_remote_token_never_reaches_the_owner_record(self):
        key = absolute("home", "u", ".ssh", "id")
        with mock.patch.object(da, "remote_head", return_value="a" * 40):   # no network in the suite
            rc, _, err = self.activate("--workdir", str(self.work), "--remote",
                                       "origin=https://u:TOKEN1@example.com/r.git?t=TOKEN2#TOKEN3",
                                       "--credential", "origin=ssh-key:" + key)
        self.assertEqual(rc, 0, err)
        owner = enrolment.find_job_owner(da.board_key(self.cfg), "J").owner
        self.assertEqual(owner["remotes"], {"origin": "https://example.com/r.git"})
        self.assertNotIn("TOKEN", json.dumps(owner))

    def test_artifact_owner_record_reads_back_with_no_review_stage(self):
        """The owner record's review keys read back through the driver's cfg semantics (the record's recipe
        and review keys overlay DEFAULT_CFG; a review launches only when review_role is truthy, as in
        driver_model.next_actions). The artifact recipe stores JSON null, never the string "none"."""
        from swarm.supervisor.driver_model import DEFAULT_CFG
        rc, _, err = self.activate("--workdir", str(self.work), "--recipe", "artifact")
        self.assertEqual(rc, 0, err)
        owner = enrolment.find_job_owner(da.board_key(self.cfg), "J")
        self.assertIn('"review_role": null', json.dumps(owner.owner))
        cfg = {**DEFAULT_CFG, "recipe": owner.owner["recipe"], "review_role": owner.owner["review_role"],
               "review_gates": owner.owner["review_gates"]}
        self.assertEqual((cfg["recipe"], cfg["review_role"], cfg["review_gates"]), ("artifact", None, None))
        self.assertFalse(cfg["review_role"])

    def test_coding_owner_record_reads_back_with_a_reviewer(self):
        from swarm.supervisor.driver_model import DEFAULT_CFG
        rc, _, err = self.activate("--workdir", str(self.work))
        self.assertEqual(rc, 0, err)
        owner = enrolment.find_job_owner(da.board_key(self.cfg), "J").owner
        cfg = {**DEFAULT_CFG, "recipe": owner["recipe"], "review_role": owner["review_role"],
               "review_gates": owner["review_gates"]}
        self.assertEqual((cfg["recipe"], cfg["review_role"]), ("coding", "reviewer"))
        self.assertTrue(cfg["review_role"])

    def test_deactivate_clears_the_drive_claim(self):
        rc, _, err = self.activate("--workdir", str(self.work))
        self.assertEqual(rc, 0, err)
        rc, _, err = self.cli("deactivate", "--job", "J", "--status", "cancelled")
        self.assertEqual(rc, 0, err)
        with self.board() as b:
            self.assertIsNone(b.job_data("J").get("drive.enabled"))

    def test_reactivation_by_the_requester_bumps_the_generation(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "sess-1"}):
            self.assertEqual(self.activate("--workdir", str(self.work))[0], 0)
            rc, _, err = self.activate("--workdir", str(self.work))
        self.assertEqual(rc, 0, err)
        led = self.ledger_of()
        self.assertEqual((led.generation, led.generation_requested), (1, 2))

    def test_reactivation_refused_for_a_seat_session(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "sess-1"}):
            self.assertEqual(self.activate("--workdir", str(self.work))[0], 0)
        FakePerson.seat_sessions = ("seat-x",)
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "seat-x"}):
            rc, _, err = self.activate("--workdir", str(self.work))
        self.assertEqual(rc, 1)
        self.assertIn("enrolled agent session", err)
        self.assertEqual(self.ledger_of().generation_requested, 1)

    def test_supervise_disable_stops_the_live_seats(self):
        self.activate("--workdir", str(self.work))
        run = {"job": "J", "unit": "unit-1.scope"}
        stop = mock.Mock()
        from swarm.supervisor import runner
        with mock.patch.object(runner, "_run_paths", return_value=[(1, Path("run-1.json")), (2, Path("run-2.json"))]), \
                mock.patch.object(runner, "_read_run", side_effect=lambda name, board: dict(run, job="J") if name == "run-1.json" else {"job": "other", "unit": "x"}), \
                mock.patch.object(runner, "_session_alive", return_value=True), \
                mock.patch.object(runner, "_stop_session", stop):
            rc, _, err = self.cli("supervise", "disable")
        self.assertEqual(rc, 0, err)
        stop.assert_called_once_with(run)

    def test_drive_budget_changes_without_rollover(self):
        self.activate("--workdir", str(self.work))
        before = self.ledger_of()
        with mock.patch.object(FakePerson, "tty", True):
            rc, _, err = self.cli("drive", "budget", "--job", "J", "max_cost_usd=30")
        self.assertEqual(rc, 0, err)
        after = self.ledger_of()
        self.assertEqual((after.budgets["max_cost_usd"], after.generation, after.generation_requested),
                         (30, before.generation, before.generation_requested))

    def test_cap_below_reserve_plus_min_session_refused(self):
        self.activate("--workdir", str(self.work))
        with mock.patch.object(FakePerson, "tty", True):
            rc, _, err = self.cli("drive", "budget", "--job", "J", "max_cost_usd=5.2")
        self.assertEqual(rc, 2)
        self.assertIn("below", err)

    def test_activation_kicks_the_supervisor_once(self):
        kicker = types.SimpleNamespace(kick=mock.Mock())
        with drivegate_stub(kicker):
            rc, _, err = self.activate("--workdir", str(self.work))
        self.assertEqual(rc, 0, err)
        kicker.kick.assert_called_once_with()

    def test_refused_activation_does_not_kick(self):
        kicker = types.SimpleNamespace(kick=mock.Mock())
        self.enable_supervisor(False)
        with drivegate_stub(kicker):
            rc, _, _ = self.activate("--workdir", str(self.work))
        self.assertEqual(rc, 1)
        kicker.kick.assert_not_called()

    def test_person_path_refused_while_the_person_module_is_absent(self):
        """Until Task 6b's swarm.person is in the build, the person path is refused, never allowed."""
        self.activate("--workdir", str(self.work))
        import swarm as swarm_package
        saved = swarm_package.__dict__.pop("person", None)   # no package attribute either: a real ImportError
        try:
            with mock.patch.dict(sys.modules, {"swarm.person": None}):
                rc, _, err = self.cli("drive", "budget", "--job", "J", "max_cost_usd=30")
        finally:
            if saved is not None:
                swarm_package.person = saved
        self.assertEqual(rc, 1)
        self.assertIn("person path", err)
        self.assertEqual(self.ledger_of().budgets["max_cost_usd"], 40)

    def test_drive_models_changes_one_role(self):
        self.activate("--workdir", str(self.work))
        with mock.patch.object(FakePerson, "tty", True):
            rc, out, err = self.cli("drive", "models", "--job", "J", "engineer=sonnet")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.ledger_of().models, {"engineer": "sonnet"})

    def test_person_path_needed_for_drive_budget(self):
        self.activate("--workdir", str(self.work))
        with mock.patch.object(FakePerson, "tty", False), \
                mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "someone-else"}):
            rc, _, err = self.cli("drive", "budget", "--job", "J", "max_cost_usd=30")
        self.assertEqual(rc, 1)
        self.assertIn("needs a person", err)

    @posix_only("supervise is POSIX-only (its systemd timer)")
    def test_supervise_disable_halts_drive_ledgers_and_enable_unhalts(self):
        self.activate("--workdir", str(self.work))
        rc, out, err = self.cli("supervise", "disable")
        self.assertEqual(rc, 0, err)
        self.assertIn("halted", out)
        self.assertEqual(self.ledger_of().halted["reason"], "supervisor disabled")
        rc, out, err = self.cli("supervise", "enable")
        self.assertEqual(rc, 0, err)
        self.assertIsNone(self.ledger_of().halted)
        self.assertGreaterEqual(self.ledger_of().halted_total_seconds, 0.0)

    def test_status_shows_effective_budget_and_models(self):
        self.activate("--workdir", str(self.work), "--model", "engineer=sonnet")
        rc, out, err = self.cli("status", "--job", "J")
        self.assertEqual(rc, 0, err)
        self.assertIn("drive budget J: cost 0.00/40.00 USD (reserve 5.00)", out)
        self.assertIn("engineer=sonnet", out)
        self.assertIn("lead=opus", out)

    @posix_only("supervise is POSIX-only (its systemd timer)")
    def test_supervise_disable_refused_for_seat_session(self):
        self.activate("--workdir", str(self.work))
        FakePerson.seat_sessions = ("seat-session",)
        with mock.patch.object(FakePerson, "tty", False), \
                mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "seat-session"}):
            rc, _, err = self.cli("supervise", "disable")
        self.assertEqual(rc, 1)
        self.assertIsNone(self.ledger_of().halted)
        self.assertIn("enabled = true", self.config.read_text())


class HaltShiftTests(HomeCase):
    def setUp(self):
        super().setUp()
        self.no_sessions = mock.patch.object(da, "stop_job_sessions", return_value=[])   # no runs here
        self.no_sessions.start()
        self.addCleanup(self.no_sessions.stop)

    def test_one_failing_ledger_does_not_stop_the_others(self):
        cfg = {"supervise": {"enabled": True}}
        with mock.patch.object(da, "board_key", return_value="bk"):
            for job in ("J1", "J2"):
                ledger.create("bk", job, budgets=da.budget_defaults({}), activated_at=NOW.isoformat())
            real_save = da._save

            def flaky(bk, job, mutate):
                if job == "J1":
                    raise da.DriveError("ledger is busy")
                return real_save(bk, job, mutate)
            with mock.patch.object(da, "_save", side_effect=flaky):
                halted, failed = da.halt_drive_jobs(cfg, actor="person", now=NOW)
        self.assertEqual(halted, ["J2"])
        self.assertEqual(failed, [("J1", "ledger is busy")])

    def test_supervise_enable_unhalts_and_shifts_deadlines(self):
        cfg = base_config()
        cfg["supervise"] = {"enabled": True}
        with mock.patch.object(da, "board_key", return_value="bk"):
            ledger.create("bk", "J", budgets=da.budget_defaults({}), activated_at=NOW.isoformat())
            t0 = NOW
            self.assertEqual(da.halt_drive_jobs(cfg, actor="person", now=t0), (["J"], []))
            self.assertEqual(da.halt_drive_jobs(cfg, actor="person", now=t0), ([], []))
            self.assertEqual(da.unhalt_drive_jobs(cfg, now=t0 + dt.timedelta(seconds=120)), (["J"], []))
            led = ledger.load("bk", "J")
            self.assertIsNone(led.halted)
            self.assertAlmostEqual(led.halted_total_seconds, 120.0)

    def test_person_halts_are_not_unhalted_by_enable(self):
        cfg = {"supervise": {"enabled": True}}
        with mock.patch.object(da, "board_key", return_value="bk"):
            ledger.create("bk", "J", budgets=da.budget_defaults({}), activated_at=NOW.isoformat())
            led = ledger.load("bk", "J")
            led.halted = {"by": "person", "at": NOW.isoformat(), "reason": "stopped by hand"}
            led.save()
            self.assertEqual(da.unhalt_drive_jobs(cfg, now=NOW), ([], []))
            self.assertEqual(ledger.load("bk", "J").halted["reason"], "stopped by hand")


def json_str(text: str) -> str:
    return json.dumps(text)


if __name__ == "__main__":
    unittest.main()
