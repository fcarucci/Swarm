"""The learnings gate of a close (mem/job-close): `swarm deactivate` is refused while an agent is unharvested or
nothing was stored for the run; `swarm learn --harvest` / `--none` clears the markers and the close goes through;
`--no-learn` is a person's waiver only."""
from test_hindsight import HindsightEnv


class CloseGateTests(HindsightEnv):
    def setUp(self):
        super().setUp()
        self.enable()
        self.assertEqual(self.cli("activate", "--job", "J", "--description", "close gate")[0], 0)

    def join(self, key):
        rc, out, err = self.cli("join", "--job", "J", "--key", key)
        self.assertEqual(rc, 0, err)
        with self.board() as b:
            return b.active_agent_name(key)

    def kill(self, key):
        """The agent ended abnormally (killed, dead, lost): no stop, no memory, no declaration."""
        with self.board() as b:
            self.assertTrue(b.close_agent(key, "killed"))

    def status(self):
        with self.board() as b:
            return b.job_status("J").status

    def test_close_is_refused_with_an_unharvested_agent(self):
        name = self.join("worker")
        self.join("reviewer")
        self.assertEqual(self.cli("remember", "--job", "J", "--as", "reviewer", "a durable fact")[0], 0)
        self.kill("worker")
        rc, out, err = self.cli("deactivate", "--job", "J")
        self.assertEqual(rc, 1, (out, err))
        self.assertIn("unharvested", err)
        self.assertIn(name, err)
        self.assertEqual(self.status(), "active")

    def test_close_is_refused_when_nothing_was_stored_for_the_run(self):
        self.join("worker")
        with self.board() as b:
            b.close_agent("worker", "done")
        rc, out, err = self.cli("deactivate", "--job", "J")
        self.assertEqual(rc, 1, (out, err))
        self.assertIn("nothing has been stored", err)
        self.assertEqual(self.status(), "active")

    def test_no_learn_without_the_person_gate_is_refused(self):
        name = self.join("worker")
        self.kill("worker")
        rc, out, err = self.cli("deactivate", "--job", "J", "--no-learn", "the person says so")
        self.assertEqual(rc, 1, (out, err))
        self.assertEqual(self.status(), "active")
        with self.board() as b:
            self.assertIsNone(b.job_data("J").get("memory.no_learn"))

    def test_after_harvest_the_close_succeeds(self):
        name = self.join("worker")
        self.kill("worker")
        rc, out, err = self.cli("learn", "--job", "J", "--none", "nothing durable came out of this run")
        self.assertEqual(rc, 0, err)
        with self.board() as b:
            self.assertEqual(b.job_data("J").get("memory.harvest") is not None, True)
            self.assertFalse([k for k in b.job_data("J") if k.startswith("memory.u.")])
        # a recorded learnings step that found nothing durable accounts for the run: no memory or waiver needed
        rc, out, err = self.cli("deactivate", "--job", "J")
        self.assertEqual(rc, 0, (out, err))
        self.assertEqual(self.status(), "completed")

    def test_harvest_lists_the_unharvested_agent(self):
        self.join("worker")
        self.kill("worker")
        rc, out, err = self.cli("learn", "--job", "J", "--harvest")
        self.assertEqual(rc, 0, err)
        self.assertIn("worker", out + err)

    def test_all_agents_declaring_none_lets_the_close_through(self):
        a, b = self.join("worker"), self.join("reviewer")
        for name in (a, b):
            rc, out, err = self.cli("remember", "--job", "J", "--as", name, "--none", "only renamed files, nothing durable")
            self.assertEqual(rc, 0, err)
        for key in ("worker", "reviewer"):
            with self.board() as bd:
                bd.agent_stopped(key)
        rc, out, err = self.cli("deactivate", "--job", "J")
        self.assertEqual(rc, 0, (out, err))
        self.assertEqual(self.status(), "completed")

    def test_a_completed_agent_without_evidence_is_still_checked_at_close(self):
        """A normal stop whose gate did not run (or failed) is not assumed harvested: the close sweep checks
        every ended agent against its stored/none evidence."""
        name = self.join("worker")
        self.assertEqual(self.cli("remember", "--job", "J", "--as", "orchestrator", "a fact")[0], 0)
        with self.board() as b:
            b.agent_stopped("worker")
        rc, out, err = self.cli("deactivate", "--job", "J")
        self.assertEqual(rc, 1, (out, err))
        self.assertIn(name, err)

    def test_a_still_active_worker_is_included_in_the_close_check(self):
        name = self.join("worker")
        self.assertEqual(self.cli("remember", "--job", "J", "--as", "orchestrator", "a fact")[0], 0)
        rc, out, err = self.cli("deactivate", "--job", "J")
        self.assertEqual(rc, 1, (out, err))
        self.assertIn(name, err)


# --- the drive learner launch (driver_model._learn_first): a terminal verdict closes only after the step ----------

import unittest  # noqa: E402

import drive_fixtures as F  # noqa: E402
from test_drive_next_actions import ARTIFEXT_CFG  # noqa: E402
from swarm.supervisor import driver_model as dm  # noqa: E402

MET = F.v("met", scope="goal", candidate="main@" + "a" * 40)


def _learn_action_id():
    acts = dm.next_actions(F.snap(goal_verdict=MET, learn_pending=True), ARTIFEXT_CFG)
    (a,) = acts
    return a


class DriverLearnLaunchTests(unittest.TestCase):
    def test_pending_learnings_launch_the_learner_before_the_close(self):
        (a,) = dm.next_actions(F.snap(goal_verdict=MET, learn_pending=True), ARTIFEXT_CFG)
        self.assertEqual((a.kind, a.prompt_kind), ("launch", "learn"))

    def test_done_learnings_let_the_close_through(self):
        (a,) = dm.next_actions(F.snap(goal_verdict=MET, learn_pending=False), ARTIFEXT_CFG)
        self.assertEqual(a.kind, "close")

    def test_a_crashed_driver_resumes_the_unconcluded_learn_launch(self):
        """The learn launch is an ordinary ledger intent: after a crash mid-harvest it is still open, so the
        next pass waits for that launch instead of launching a second learner or closing."""
        action_id = _learn_action_id().action_id
        open_intent = F.intent(state="running", launch="learner", action_id=action_id, launch_id="L1")
        s = F.snap(goal_verdict=MET, learn_pending=True, ledger=F.L(intents=[open_intent]))
        (a,) = dm.next_actions(s, ARTIFEXT_CFG)
        self.assertEqual((a.kind, a.responsible), ("nothing", "learner"))

    def test_exhausted_learn_attempts_hold_the_job_open_instead_of_closing(self):
        action_id = _learn_action_id().action_id
        failed = [F.intent(state="concluded", outcome="error", success=False, launch="learner",
                           action_id=action_id, launch_id=f"L{i}") for i in range(ARTIFEXT_CFG["max_attempts"])]
        s = F.snap(goal_verdict=MET, learn_pending=True, ledger=F.L(intents=failed))
        (a,) = dm.next_actions(s, ARTIFEXT_CFG)
        self.assertEqual(a.kind, "hold")
        self.assertIn("not closing", a.reason)


class LearnLaunchProtocolTests(unittest.TestCase):
    """The learner launch's `swarm learn` goes through the broker's typed schema: harvest, --from, --none."""

    def test_learn_verbs_parse_and_build(self):
        from swarm import launchproto
        req = launchproto.parse_cli(["learn", "--harvest"], ".")
        self.assertEqual(req["command"], "learn")
        self.assertTrue(req["fields"]["harvest"])
        req = launchproto.parse_cli(["learn", "--none", "nothing durable came out of this run"], ".")
        cmd, fields = launchproto.validate(req, "J")
        self.assertEqual((cmd, fields["job"]), ("learn", "J"))
        argv = launchproto.build_argv(cmd, fields, key="k", name="n", file_path=lambda c: "/x")
        self.assertIn("--none=nothing durable came out of this run", argv)
        self.assertEqual(argv[0:1], ["learn"])

    def test_the_learner_prompt_command_sequence_goes_through_the_broker(self):
        """Every `swarm learn` the learner prompt asks for parses on the launch side, validates with the launch's
        own job filled in by the broker, and builds a CLI argv the CLI's own parser accepts."""
        import os
        import tempfile
        from swarm import cli, launchproto
        with tempfile.TemporaryDirectory(dir=os.path.dirname(__file__)) as d:
            facts = os.path.join(d, "facts.txt")
            with open(facts, "w") as fh:
                fh.write("one durable fact\n")
            for argv in (["learn", "--list-banks"], ["learn", "--harvest"],
                         ["learn", "--bank", "coding", "--from", "facts.txt"],
                         ["learn", "--none", "nothing durable came out of this run"]):
                with self.subTest(argv=argv):
                    req = launchproto.parse_cli(argv, d)
                    cmd, fields = launchproto.validate(req, "J")
                    built = launchproto.build_argv(cmd, fields, key="k", name="n", file_path=lambda c: facts)
                    ns = cli._parser().parse_args(built)
                    self.assertEqual((ns.cmd, ns.job), ("learn", "J"))

    def test_a_launch_may_run_learn_remember_and_recall(self):
        """The launch's settings allow the memory commands (launchhome.LAUNCH_ALLOW): a learner's `swarm learn
        --harvest`, `--from` and `--none`, and every launch's `swarm remember` (the stop gate) are not denied."""
        import fnmatch
        from swarm.supervisor import launchhome
        rules = [r[len("Bash("):-1].replace(":*", "*") for r in launchhome.LAUNCH_ALLOW]
        for cmd in ("swarm learn --harvest", "swarm learn --bank coding --from facts.txt",
                    "swarm learn --none nothing durable here", "swarm learn --list-banks",
                    "swarm remember --none nothing worth keeping", "swarm recall why did it fail"):
            with self.subTest(cmd=cmd):
                self.assertTrue(any(fnmatch.fnmatchcase(cmd, r) for r in rules), cmd)

    def test_learn_prompt_names_the_harvest_and_the_retain_steps(self):
        from types import SimpleNamespace
        from swarm.supervisor import prompts
        out = "\n".join(prompts._learn_body(SimpleNamespace(job="J"), None, None))
        self.assertIn("swarm learn --job J --harvest", out)
        self.assertIn("--from FILE", out)
        self.assertIn("--none", out)


class DriverLearnFailClosedTests(unittest.TestCase):
    """A failed learnings read keeps the step pending; a verdict held for its learnings is never closed by a
    deadline (the ledger-only dispatcher reads the hold the pass wrote)."""

    def test_a_driver_read_failure_keeps_learn_pending(self):
        from unittest import mock
        from types import SimpleNamespace
        from swarm import learnings
        from swarm.supervisor import driver
        snap = F.snap(goal_verdict=MET)
        with mock.patch.object(learnings, "learn_pending", side_effect=RuntimeError("board down")):
            out = driver._with_learn(None, {}, SimpleNamespace(job="J"), snap)
        self.assertTrue(out.learn_pending)
        (a,) = dm.next_actions(out, ARTIFEXT_CFG)
        self.assertNotEqual(a.kind, "close")

    def test_the_pass_records_the_hold_and_clears_it_when_done(self):
        from types import SimpleNamespace
        from swarm.supervisor import driver, driver_life
        led = SimpleNamespace(watermarks={}, save=lambda: None)
        m = SimpleNamespace(life=driver_life)
        pending = F.snap(goal_verdict=MET, learn_pending=True)
        driver._note_learn_hold(led, pending, dm.next_actions(pending, ARTIFEXT_CFG), m)
        self.assertIn(driver_life.LEARN_HOLD, led.watermarks)
        done = F.snap(goal_verdict=MET, learn_pending=False)
        driver._note_learn_hold(led, done, dm.next_actions(done, ARTIFEXT_CFG), m)
        self.assertNotIn(driver_life.LEARN_HOLD, led.watermarks)


class ClosePerAgentEvidenceTests(CloseGateTests):
    """Round 2: coverage is per agent (Y1), a marker clears on the agent's own evidence (Y2), a legacy job's
    tags can be cleared (2b), a scope that cannot be saved fails the activation (Y3)."""

    def test_an_agent_not_covered_by_the_learnings_step_is_still_harvested(self):
        from swarm import learnings
        self.join("worker")
        self.kill("worker")
        with self.board() as b:
            learnings.sweep(b, "J")
            pending = learnings.unharvested(b, "J")      # what the step covers: the worker only
        late = self.join("late")                         # joins while the step is retaining
        with self.board() as b:
            b.agent_stopped("late")
            learnings.harvest(b, "J", "test", 1, None, pending)
            self.assertEqual([e["name"] for e in learnings.unharvested(b, "J")], [])
            learnings.sweep(b, "J")
            self.assertEqual([e["name"] for e in learnings.unharvested(b, "J")], [late])

    def test_a_marker_from_a_refused_close_clears_once_the_agent_declares_none(self):
        name = self.join("worker")
        rc, out, err = self.cli("deactivate", "--job", "J")
        self.assertEqual(rc, 1, (out, err))
        self.assertIn(name, err)                          # still working at the refused close: marked
        rc, out, err = self.cli("remember", "--job", "J", "--as", name, "--none", "only renamed files here")
        self.assertEqual(rc, 0, err)
        with self.board() as b:
            from swarm import learnings
            self.assertEqual(learnings.unharvested(b, "J"), [])
            b.agent_stopped("worker")
        rc, out, err = self.cli("deactivate", "--job", "J")
        self.assertEqual(rc, 0, (out, err))

    def test_a_marker_clears_once_the_agent_stores_a_memory(self):
        name = self.join("worker")
        self.assertEqual(self.cli("deactivate", "--job", "J")[0], 1)
        self.assertEqual(self.cli("remember", "--job", "J", "--as", name, "a durable fact")[0], 0)
        with self.board() as b:
            from swarm import learnings
            self.assertEqual(learnings.unharvested(b, "J"), [])

    def test_a_scope_that_cannot_be_saved_fails_the_activation(self):
        from types import SimpleNamespace
        from swarm import learn_cli
        args = SimpleNamespace(job="NoSuchJob", project=None, topic="what it is about")
        self.assertEqual(learn_cli.record_memory_scope(self.cfg, args, ["swarm"], "/"), 1)


class LegacyTagsTests(HindsightEnv):
    def setUp(self):
        super().setUp()
        self.enable()
        self.fake.banks["proj"] = []
        self.assertEqual(self.cli("activate", "--job", "J", "--project", "proj")[0], 0)
        with self.board() as b:
            b.set_job_data("J", "memory.tags", None)       # a job from before subject tags: never set

    def remembered_tags(self):
        rc, _, err = self.cli("remember", "--job", "J", "--as", "Homer Simpson", "a fact")
        self.assertEqual(rc, 0, err)
        return self.fake.calls("POST", "/v1/default/banks/proj/memories")[-1]["body"]["items"][0]["tags"]

    def test_set_empty_on_a_legacy_job_clears_it(self):
        self.assertEqual(self.remembered_tags(), ["proj"])
        self.assertEqual(self.cli("job", "tags", "--job", "J", "--set", "")[0], 0)
        self.assertEqual(self.remembered_tags(), [])

    def test_removing_the_implicit_project_tag_clears_it(self):
        self.assertEqual(self.cli("job", "tags", "--job", "J", "--remove", "proj")[0], 0)
        self.assertEqual(self.remembered_tags(), [])

    def test_adding_to_a_legacy_job_keeps_its_project_tag(self):
        self.assertEqual(self.cli("job", "tags", "--job", "J", "--add", "swarm")[0], 0)
        self.assertEqual(self.remembered_tags(), ["proj", "swarm"])


from test_drive_pass import PassCase  # noqa: E402


class LearnHoldAcrossTheDeadlineTests(PassCase):
    """Multi-pass, through the real driver, model and deadline dispatcher: a MET whose learner launch ran out of
    attempts holds; the pass records the hold in the ledger; a later pass past every deadline neither records
    UNREACHABLE nor closes."""

    def test_an_unlanded_met_gets_no_learn_hold(self):
        """No hold unless the model actually holds the close for learnings: the deadline still applies."""
        from unittest import mock
        from swarm import learnings
        from swarm.supervisor import driver_life
        self.job()
        self.mods.model = dm
        self.snap.read = lambda board, l, job, *, cfg, now: F.snap(job=job, goal_verdict=MET, ledger=l, now=now)
        with mock.patch.object(learnings, "learn_pending", return_value=True):
            self.drive(now=self.t0)
        self.assertNotIn(driver_life.LEARN_HOLD, self.load().watermarks)


    def test_an_exhausted_learner_holds_the_met_across_the_deadline(self):
        import datetime as dt
        from unittest import mock
        from swarm import learnings
        from swarm.supervisor import driver_life, ledger as ledger_mod
        led = self.job()
        action_id = _learn_action_id().action_id
        led.intents = [ledger_mod.Intent(launch_id=f"L{i}", action_id=action_id, attempt=i + 1, launch="learner",
                                         reserved_usd=0.0, reserved_minutes=0.0, locks=[], state="concluded",
                                         outcome="error", success=False, charged_usd=0.0)
                       for i in range(dm.DEFAULT_CFG["max_attempts"])]
        led.save()
        self.mods.model = dm
        landed = {"main": {"landed": "a" * 40}}                       # the judged candidate landed: MET closes
        self.snap.read = lambda board, l, job, *, cfg, now: F.snap(job=job, goal_verdict=MET, ledger=l, now=now,
                                                                   integrated=landed)
        with mock.patch.object(learnings, "learn_pending", return_value=True), \
                mock.patch.object(driver_life, "terminalize", side_effect=AssertionError("never closes")):
            self.drive(now=self.t0)                                   # pass 1: hold, recorded in the ledger
            self.assertIn(driver_life.LEARN_HOLD, self.load().watermarks)
            later = self.t0 + dt.timedelta(days=30)                   # past the wall-clock and ruling deadlines
            self.assertIsNotNone(driver_life._deadline_cause({}, self.load(), later))
            self.drive(now=later)                                     # pass 2: the deadline dispatcher runs first
        led = self.load()
        self.assertIsNone(led.terminal_intent)
        self.assertIsNone(led.terminal)
        self.assertIn(driver_life.LEARN_HOLD, led.watermarks)
        self.assertEqual(self.board.job_status("J").status, "active")
        self.assertEqual(self.exec.launched, [])                      # no fourth learner
