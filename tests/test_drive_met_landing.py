"""F03e: landing depends on pinned destinations; MET cannot become unreachable."""
import datetime as dt
import unittest
from types import SimpleNamespace
from unittest import mock

import support  # noqa: F401
from drive_fixtures import L, T0, ho, intent, snap, v
from swarm import land
from swarm.supervisor import driver, driver_model as dm, driver_snapshot as ds, driver_life as life

ART = "feat/a@" + "a" * 40


class MetLanding(unittest.TestCase):
    def snapshot(self, scope="goal", **kw):
        return snap(goal_verdict=v("met", scope=scope, candidate=ART), handoffs=[ho(ART)], **kw)

    def test_destination_free_met_closes_after_learnings_for_both_scopes_and_recipes(self):
        for scope in ("goal", "ruling"):
            for recipe in ("coding", "artifact"):
                cfg = {"recipe": recipe, "landing_required": False}
                (a,) = dm.next_actions(self.snapshot(scope, learn_pending=True), cfg)
                self.assertEqual((a.kind, a.prompt_kind), ("launch", "learn"))
                (a,) = dm.next_actions(self.snapshot(scope), cfg)
                self.assertEqual((a.kind, a.reason), ("close", "met"))

    def test_destination_free_artifact_met_never_queues_integrator(self):
        s = snap(handoffs=[ho(ART)], verdicts={ART: v("met")},
                 goal_ready={"id": 3, "candidate": ART, "req": "r", "at": T0})
        (a,) = dm.next_actions(s, {"landing_required": False})
        self.assertEqual(a.prompt_kind, "goal")

    def test_pinned_met_retries_with_bound_then_escalates_on_backoff(self):
        for scope in ("goal", "ruling"):
            for recipe in ("coding", "artifact"):
                cfg = {"landing_required": True, "recipe": recipe, "max_attempts": 2}
                led = L(ruling_due_at=(T0 - dt.timedelta(days=2)).isoformat(), ruling_attempts=99)
                s = self.snapshot(scope, ledger=led)
                for n in range(2):
                    (a,) = dm.next_actions(s, cfg)
                    self.assertEqual((a.kind, a.prompt_kind, a.artifact), ("launch", "integrate", ART))
                    led.intents.append(intent(action_id=a.action_id, outcome="refused", launch="integrator"))
                    (a,) = dm.next_actions(s, cfg)
                    self.assertEqual(a.kind, "escalate_landing")
                    self.assertIn("met, landing blocked: refused", a.reason)
                    led.save = lambda: None
                    board = mock.Mock()
                    driver._escalate_landing(board, led, s, s.now, a.reason)
                    self.assertEqual(board.post_event.call_args.args[1], "launch-failed")
                    self.assertIn("DRIVER-ESCALATE met, landing blocked", board.post.call_args.args[2])
                    self.assertEqual(dm.next_actions(s, cfg)[0].kind, "hold")
                    s = snap(**{**{k: getattr(s, k) for k in s.__dataclass_fields__},
                                "now": s.now + dt.timedelta(seconds=301)})
                (a,) = dm.next_actions(s, cfg)
                self.assertEqual(a.kind, "escalate_landing")
                self.assertEqual(len(led.intents), 2)
                self.assertIsNone(led.terminal)

    def test_pinned_met_awaits_running_integrator(self):
        led = L(intents=[intent(state="claimed", action_id="gen1|integrate|-|feat/a|0|1|retry0")])
        (a,) = dm.next_actions(self.snapshot(ledger=led), {"landing_required": True})
        self.assertEqual((a.kind, a.responsible), ("nothing", "integrator"))

    def test_outage_can_requeue_integrator_without_consuming_an_attempt(self):
        s = self.snapshot()
        (first,) = dm.next_actions(s, {"landing_required": True, "max_attempts": 1})
        s.ledger.intents.append(intent(action_id=first.action_id, outcome="outage"))
        (retry,) = dm.next_actions(s, {"landing_required": True, "max_attempts": 1})
        self.assertEqual(retry.prompt_kind, "integrate")
        self.assertNotEqual(first.action_id, retry.action_id)

    def test_refused_integrator_reservation_uses_escalation_path(self):
        import time
        s = self.snapshot()
        s.ledger.save = lambda: None
        (action,) = dm.next_actions(s, {"landing_required": True})
        exe = mock.Mock()
        exe.execute.return_value = None
        exe.reservation_reason.return_value = "session budget exhausted"
        board = mock.Mock()
        driver._act(board, {}, {}, s.ledger, s, [action], T0, None, "", time.monotonic() + 10,
                    SimpleNamespace(exec=exe))
        self.assertIn("met, landing blocked: session budget exhausted", board.post.call_args.args[2])
        self.assertIsNone(s.ledger.terminal)

    def test_owner_destinations_override_recipe_and_live_config(self):
        for recipe in ("coding", "artifact"):
            for remotes in ({}, {"origin": "/repo.git"}):
                owner = {"recipe": recipe, "review_role": None, "review_gates": None,
                         "target_branch": "main", "remotes": remotes}
                with mock.patch("swarm.enrolment.find_job_owner", return_value=SimpleNamespace(owner=owner)):
                    cfg = ds.drive_cfg("bk", "J", {"landing_required": not bool(remotes)})
                self.assertEqual(cfg["landing_required"], bool(remotes))
        self.assertFalse(land.landing_required({"target_branch": "main"}))

    def test_deadline_never_overrides_receipted_met_of_either_scope(self):
        for scope in ("goal", "ruling"):
            s = self.snapshot(scope)
            led = s.ledger
            led.ruling_due_at = (T0 - dt.timedelta(days=2)).isoformat()
            board = mock.Mock()
            board.job_status.return_value = SimpleNamespace(verdict_scope="artifact", verdict="not_met", verdict_generation=1)
            with mock.patch.object(ds, "read", return_value=s):
                self.assertFalse(life.enforce_deadlines(board, {}, led, T0))
            self.assertIsNone(led.terminal_intent)

    def test_status_names_destination_free_landing(self):
        from swarm import drive_activate
        owner = SimpleNamespace(owner={"remotes": {}})
        with mock.patch.object(drive_activate.ledger, "load", return_value=L(models={})), \
                mock.patch.object(drive_activate, "board_key", return_value="bk"), \
                mock.patch("swarm.enrolment.find_job_owner", return_value=owner):
            self.assertIn("landing: none (no destination pinned)", drive_activate.drive_lines({}, "J"))

    def test_permanent_no_destination_has_no_unfinished_journal(self):
        from swarm.supervisor import driver_land
        env = SimpleNamespace(owner={"remotes": {}}, now=lambda: T0)
        board = mock.Mock()
        self.assertEqual(driver_land.complete_all(board, {}, env, "J"), [])
        board.assert_not_called()
