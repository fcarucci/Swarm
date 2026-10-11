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

    def test_remote_urls_never_store_userinfo(self):
        self.assertEqual(da._strip_userinfo("https://user:TOKEN@example.com/o/r.git"),
                         "https://example.com/o/r.git")

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
        self.platform = mock.patch.object(da, "PLATFORM", "linux")   # the timer check is for Linux here
        self.platform.start()
        self.addCleanup(self.platform.stop)
        self.timer = mock.patch.object(systemd, "timer_state", return_value=dict(ACTIVE_TIMER))
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
        p = mock.patch.object(sup_settings, "load_state", return_value={"last_run_at": now})
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

    def test_review_gates_standard_when_the_engineer_is_not_haiku(self):
        rc, _, err = self.activate("--workdir", str(self.work), "--model", "engineer=sonnet")
        self.assertEqual(rc, 0, err)
        self.assertEqual(enrolment.find_job_owner(da.board_key(self.cfg), "J").owner["review_gates"], "standard")

    def test_artifact_recipe_has_no_review_stage(self):
        rc, _, err = self.activate("--workdir", str(self.work), "--recipe", "artifact")
        self.assertEqual(rc, 0, err)
        owner = enrolment.find_job_owner(da.board_key(self.cfg), "J").owner
        self.assertEqual((owner["recipe"], owner["review_role"]), ("artifact", "none"))

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
        with mock.patch.object(systemd, "timer_state", return_value={"ActiveState": "inactive"}):
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

    def test_activating_ledger_completed_by_next_pass(self):
        """A crash after each durable step: the next pass (finish_activation) completes the job, and
        nothing is duplicated. A crash before the ledger exists leaves nothing to complete; the activation
        is then run again and finishes. Each case has its own job name."""
        for i, point in enumerate(("brief", "owner", "ledger", "board", "lead")):
            job = f"J{i}"
            with self.subTest(crash=point):
                plan = self.plan(job=job)
                with self.board() as b:
                    with self.assertRaises(RuntimeError):
                        if point in ("brief", "owner", "ledger"):
                            da.activate_drive(self.cfg, b, plan, crash_after=point)
                        else:
                            da.activate_drive(self.cfg, b, plan)
                            da.finish_activation(self.cfg, b, job, crash_after=point)
                    if point in ("brief", "owner"):
                        self.assertIsNone(self.ledger_of(job))
                        da.activate_drive(self.cfg, b, plan)          # the person runs it again
                    da.finish_activation(self.cfg, b, job)           # the next pass
                    da.finish_activation(self.cfg, b, job)           # and again: a no-op
                    self.assertEqual(self.ledger_of(job).state, "active")
                    self.assertEqual(len(b.seats(job, 1)), 1)
                    self.assertEqual(b.job_status(job).status, "active")

    def test_drive_budget_changes_without_rollover(self):
        self.activate("--workdir", str(self.work))
        before = self.ledger_of()
        with mock.patch.object(da, "_tty", return_value=True):
            rc, _, err = self.cli("drive", "budget", "--job", "J", "max_cost_usd=30")
        self.assertEqual(rc, 0, err)
        after = self.ledger_of()
        self.assertEqual((after.budgets["max_cost_usd"], after.generation, after.generation_requested),
                         (30, before.generation, before.generation_requested))

    def test_cap_below_reserve_plus_min_session_refused(self):
        self.activate("--workdir", str(self.work))
        with mock.patch.object(da, "_tty", return_value=True):
            rc, _, err = self.cli("drive", "budget", "--job", "J", "max_cost_usd=5.2")
        self.assertEqual(rc, 2)
        self.assertIn("below", err)

    def test_drive_models_changes_one_role(self):
        self.activate("--workdir", str(self.work))
        with mock.patch.object(da, "_tty", return_value=True):
            rc, out, err = self.cli("drive", "models", "--job", "J", "engineer=sonnet")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.ledger_of().models, {"engineer": "sonnet"})

    def test_person_path_needed_for_drive_budget(self):
        self.activate("--workdir", str(self.work))
        with mock.patch.object(da, "_tty", return_value=False), \
                mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "someone-else"}):
            rc, _, err = self.cli("drive", "budget", "--job", "J", "max_cost_usd=30")
        self.assertEqual(rc, 1)
        self.assertIn("requester session", err)

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
        with mock.patch.object(da, "_tty", return_value=False), \
                mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "seat-session"}):
            rc, _, err = self.cli("supervise", "disable")
        self.assertEqual(rc, 1)
        self.assertIsNone(self.ledger_of().halted)
        self.assertIn("enabled = true", self.config.read_text())


class HaltShiftTests(HomeCase):
    def test_supervise_enable_unhalts_and_shifts_deadlines(self):
        cfg = base_config()
        cfg["supervise"] = {"enabled": True}
        with mock.patch.object(da, "board_key", return_value="bk"):
            ledger.create("bk", "J", budgets=da.budget_defaults({}), activated_at=NOW.isoformat())
            t0 = NOW
            self.assertEqual(da.halt_drive_jobs(cfg, actor="person", now=t0), ["J"])
            self.assertEqual(da.halt_drive_jobs(cfg, actor="person", now=t0), [])
            self.assertEqual(da.unhalt_drive_jobs(cfg, now=t0 + dt.timedelta(seconds=120)), ["J"])
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
            self.assertEqual(da.unhalt_drive_jobs(cfg, now=NOW), [])
            self.assertEqual(ledger.load("bk", "J").halted["reason"], "stopped by hand")


def json_str(text: str) -> str:
    return json.dumps(text)


if __name__ == "__main__":
    unittest.main()
