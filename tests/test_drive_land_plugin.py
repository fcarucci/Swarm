"""Task 11b: the engineering-team plugin in a drive job: the integrator brief ends with `swarm land`, and
integration is read from the host-private ledger, not from git facts."""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import support  # noqa: F401
from swarm.supervisor import ledger
from test_team_land import load

SHA = "a" * 40
ART = f"feat/x@{SHA}"
BK = "bk"


class FakeBoard:
    def __init__(self, data):
        self.data = data

    def job_data(self, job):
        if isinstance(self.data, Exception):
            raise self.data
        return self.data

    def job_status(self, job):
        return mock.Mock(job=job)


class PluginCase(unittest.TestCase):
    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="landplugin-", dir=support.SANDBOX))
        self.addCleanup(shutil.rmtree, self.root, True)
        p = mock.patch.dict(os.environ, {**support.home_env(self.root), "SWARM_TEAM_CONFIG": ""})
        p.start()
        self.addCleanup(p.stop)
        self.sp = load("swarm_plugin")
        self.ctx = type("C", (), {"config_dir": Path(self.root), "cfg": {}})()
        store = mock.patch("swarm.board.autoinit.store_key", lambda cfg: BK)
        store.start()
        self.addCleanup(store.stop)
        rec = mock.patch("swarm.supervisor.orphans.local_record", lambda cfg, js: None)
        rec.start()
        self.addCleanup(rec.stop)

    def ledger(self, **integrated):
        led = ledger.create(BK, "J", budgets={}, activated_at="2026-10-11T00:00:00+00:00")
        led.integrated.update(integrated)
        led.save()
        return led


class DriveJob(PluginCase):
    def test_drive_job_follows_the_host_private_ledger_not_the_board_claim(self):
        self.assertFalse(self.sp.drive_job(self.ctx, "J"))
        self.ledger()
        self.assertTrue(self.sp.drive_job(self.ctx, "J"))
        self.assertFalse(self.sp.drive_job(self.ctx, "K"))

    def test_a_bad_ledger_is_still_a_drive_job(self):
        self.ledger()
        path = ledger_path("J")
        with open(path, "w") as fh:
            fh.write("{not a ledger")
        self.assertTrue(self.sp.drive_job(self.ctx, "J"))

    def test_a_cleared_board_claim_cannot_turn_a_drive_job_into_git_inference(self):
        self.ledger()
        board = FakeBoard({})                         # the claim is gone: agents can write it
        with mock.patch.object(self.sp, "coding_integrated", side_effect=AssertionError("git facts used")):
            recipe = self.sp.coding_recipe(self.ctx, board, "J", ART)
        self.assertIn("swarm land --job J", recipe["finalize"])

    def test_landed_only_from_the_ledger_and_the_exact_sha(self):
        self.assertFalse(self.sp.drive_landed(self.ctx, "J", "feat/x", SHA))        # no ledger
        self.ledger(**{"feat/x": {"landed": SHA}})
        self.assertTrue(self.sp.drive_landed(self.ctx, "J", "feat/x", SHA))
        self.assertTrue(self.sp.drive_landed(self.ctx, "J", "feat/x", SHA.upper()))
        self.assertFalse(self.sp.drive_landed(self.ctx, "J", "feat/x", "b" * 40))
        self.assertFalse(self.sp.drive_landed(self.ctx, "J", "feat/y", SHA))
        self.assertFalse(self.sp.drive_landed(self.ctx, "K", "feat/x", SHA))

    def test_drive_recipe_ends_with_swarm_land_and_never_pushes_the_target(self):
        self.ledger()
        board = FakeBoard({})
        with mock.patch.object(self.sp, "coding_integrated", side_effect=AssertionError("git facts used")):
            recipe = self.sp.coding_recipe(self.ctx, board, "J", ART)
        text = recipe["finalize"]
        self.assertIn(f"swarm land --job J --artifact {ART}", text)
        self.assertIn("Never push to the target branch main", text)
        self.assertIn("never post INTEGRATED", text)
        self.assertNotIn("Advance the target only by fast-forward", text)
        self.assertNotIn("then post INTEGRATED", text)
        self.assertIn("swarm ci wait", text)
        self.assertEqual(recipe["finalizer_role"], "integrator")
        self.assertEqual(recipe["artifact_group"], "feat/x")

    def test_drive_recipe_is_integrated_only_when_the_ledger_says_landed(self):
        self.ledger()
        board = FakeBoard({})
        with mock.patch.object(self.sp, "coding_integrated", return_value=True):
            self.assertNotIn("integrated", self.sp.coding_recipe(self.ctx, board, "J", ART))
        led = ledger.load(BK, "J")
        led.integrated["feat/x"] = {"landed": SHA}
        led.save()
        self.assertEqual(self.sp.coding_recipe(self.ctx, board, "J", ART), {"artifact_group": "feat/x", "integrated": True})

    def test_non_drive_recipe_is_unchanged(self):
        board = FakeBoard({})
        recipe = self.sp.coding_recipe(self.ctx, board, "J", ART)
        self.assertIn("then post INTEGRATED", recipe["finalize"])
        self.assertNotIn("swarm land", recipe["finalize"])
        with mock.patch.object(self.sp, "coding_integrated", return_value=True) as ci:
            self.assertEqual(self.sp.coding_recipe(self.ctx, board, "J", ART)["integrated"], True)
        ci.assert_called_once()

    def test_squash_strategy_text_in_drive_mode(self):
        self.ledger()
        (Path(self.root) / "team.toml").write_text('[land]\nstrategy = "squash-ff"\n')
        recipe = self.sp.coding_recipe(self.ctx, FakeBoard({}), "J", ART)
        self.assertIn("squash-ff", recipe["finalize"])
        self.assertIn("swarm land --job J", recipe["finalize"])


def ledger_path(job):
    from swarm.supervisor import settings
    return settings.private_dir() / "drive" / ledger._name(BK, job)


if __name__ == "__main__":
    unittest.main()
