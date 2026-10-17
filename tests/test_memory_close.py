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

