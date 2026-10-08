"""`swarm learn --job J` followed by `swarm deactivate --job J` (no --force) must close the job."""
from test_hindsight import HindsightEnv


class LearnThenDeactivateTests(HindsightEnv):
    def setUp(self):
        super().setUp()
        self.enable()

    def learn_then_deactivate(self, bank, *activate_args):
        self.assertEqual(self.cli("activate", "--job", "J", *activate_args)[0], 0)
        rc, out, err = self.cli("learn", "--job", "J", "--bank", bank, "-", stdin="A self-contained fact.\n")
        self.assertEqual(rc, 0, err)
        self.assertIn(f'learned in bank "{bank}"', out)
        rc, out, err = self.cli("deactivate", "--job", "J")
        self.assertEqual(rc, 0, err)
        with self.board() as b:
            s = b.job_status("J")
        self.assertEqual(s.status, "completed", (out, err))
        return out

    def test_deactivate_after_learn_in_the_default_bank(self):
        out = self.learn_then_deactivate("coding")
        self.assertIn("deactivated J (completed)", out)

    def test_deactivate_after_learn_in_another_bank_and_project(self):
        self.learn_then_deactivate("hermes", "--project", "job-bank")

    def test_deactivate_after_learn_on_a_goal_job_with_met_verdict(self):
        self.assertEqual(self.cli("activate", "--job", "J", "--goal", "ship it")[0], 0)
        self.cli("join", "--job", "J", "--key", "j")
        with self.board() as b:
            name = b.active_agent_name("j")
            b.claim_judge("j", "J")
            b.record_verdict("J", name, "met", "ok")
        rc, _, err = self.cli("learn", "--job", "J", "--bank", "coding", "-", stdin="Fact.\n")
        self.assertEqual(rc, 0, err)
        rc, out, err = self.cli("deactivate", "--job", "J")
        self.assertEqual(rc, 0, err)
        with self.board() as b:
            self.assertEqual(b.job_status("J").status, "completed")
