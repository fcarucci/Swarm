"""`swarm deactivate --status completed` without --force when the judge's met verdict (candidate REF@SHA)
covers the latest hand-off (a bare SHA or report:<path>@<blob>), for the current generation only."""
from types import SimpleNamespace
from unittest import mock

from test_hindsight import HindsightEnv

SHA = "a1b2c3d4" * 5
BLOB = "9f8e7d6c" * 5


def verdict(verdict="met", candidate=f"main@{SHA}", generation=2):
    return SimpleNamespace(verdict=verdict, candidate=candidate, generation=generation)


class DeactivateMetTests(HindsightEnv):
    def setUp(self):
        super().setUp()
        self.enable()
        self.assertEqual(self.cli("activate", "--job", "J", "--no-drive", "--reason", "legacy goal test",
                                  "--goal", "ship it")[0], 0)
        with self.board() as b:
            b.set_job_data("J", "drive.enabled", "1")   # the verdict comes from the drive snapshot
        rc, _, err = self.cli("learn", "--job", "J", "--bank", "coding", "-", stdin="Fact.\n")
        self.assertEqual(rc, 0, err)

    def deactivate(self, gv, handoffs, *extra):
        snap = SimpleNamespace(goal_verdict=gv, handoffs=[SimpleNamespace(artifact=a) for a in handoffs])
        with mock.patch("swarm.supervisor.ledger.load", return_value=SimpleNamespace(generation=2)), \
                mock.patch("swarm.supervisor.driver_snapshot.read", return_value=snap):
            return self.cli("deactivate", "--job", "J", "--status", "completed", *extra)

    def status(self):
        with self.board() as b:
            return b.job_status("J")

    def test_met_ruling_candidate_ref_at_sha_covers_a_bare_sha_hand_off(self):
        rc, out, err = self.deactivate(verdict(candidate=f"main@{SHA}"), [SHA])
        self.assertEqual(rc, 0, err)
        js = self.status()
        self.assertEqual(js.status, "completed")
        self.assertFalse(js.completion_forced)

    def test_met_goal_candidate_covers_a_report_artifact_with_the_same_blob(self):
        rc, out, err = self.deactivate(verdict(candidate=f"report:docs/x.html@{BLOB}"),
                                       [f"report:docs/x.html@{BLOB}"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.status().status, "completed")

    def test_an_abbreviated_sha_matches(self):
        rc, _, err = self.deactivate(verdict(candidate=f"main@{SHA}"), [SHA[:9]])
        self.assertEqual(rc, 0, err)

    def test_a_met_verdict_of_an_older_generation_is_refused(self):
        rc, _, err = self.deactivate(verdict(generation=1), [SHA])
        self.assertEqual(rc, 1)
        self.assertIn("not completing J", err)
        self.assertEqual(self.status().status, "active")

    def test_a_met_verdict_for_another_head_is_refused(self):
        rc, _, err = self.deactivate(verdict(candidate="main@" + "0" * 40), [SHA])
        self.assertEqual(rc, 1)
        self.assertEqual(self.status().status, "active")

    def test_not_met_is_refused(self):
        rc, _, err = self.deactivate(verdict("not_met"), [SHA])
        self.assertEqual(rc, 1)
        self.assertIn("not completing J", err)
        self.assertEqual(self.status().status, "active")


class LearningsGateMessageTests(HindsightEnv):
    def test_the_gate_lists_the_exact_commands(self):
        self.enable()
        self.assertEqual(self.cli("activate", "--job", "J")[0], 0)
        rc, _, err = self.cli("deactivate", "--job", "J")
        self.assertEqual(rc, 1)
        for command in ("swarm learn --job J --bank BANK -",
                        'swarm learn --job J --none "<reason>"',
                        'swarm deactivate --job J --status completed --no-learn "<reason>"'):
            self.assertIn(command, err)
