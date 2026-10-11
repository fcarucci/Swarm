"""The pure driver decision `next_actions` (spec 3.2-3.3, 3.5-3.7, 3.9-3.10, 4.1) and the progress rule
of spec 3.6. Table tests over Snapshot values built by drive_fixtures; no board, no I/O.

Action ids in these tests follow `gen<N>|kind|seat|group|round|event` (see driver_model.next_actions)."""
from __future__ import annotations

import unittest

import support  # noqa: F401  (puts lib/ on sys.path)
from drive_fixtures import L, T0, ho, intent, iso, minutes, q, seat, snap, v
from swarm.supervisor import driver_model as dm
from swarm.supervisor.driver_model import NO_PROGRESS_REASONS, next_actions, progress

CFG = {
    "max_sessions": 60, "max_seats": 12, "max_rounds": 8, "no_progress_rounds": 3, "max_attempts": 3,
    "max_cost_usd": 40, "ruling_reserve_usd": 5, "session_usd_cap": 5, "min_session_usd": 0.5,
    "max_session_minutes": 60, "max_wall_hours": 24, "ruling_attempts": 2, "ruling_deadline_minutes": 120,
    "ruling_grace_minutes": 15, "question_expiry": "4h", "stall_minutes": 90, "driver_error_passes": 6,
    "max_concurrent_sessions": 4, "lease_hold_minutes": 60, "recipe": "coding", "review_role": "reviewer",
}
ARTIFEXT_CFG = {**CFG, "recipe": "artifact", "review_role": None}

A = "feat/a@" + "a" * 40          # artifact of group feat/a
B = "feat/b@" + "b" * 40          # artifact of group feat/b
R = "report:x@" + "c" * 40        # artifact of group report:x (artifact recipe)
LEAD = dict(role="engineering_lead")


def kinds(acts):
    return [x.kind for x in acts]


def named(acts):
    return [(x.kind, x.seat, x.prompt_kind) for x in acts]


def stuck_intents(action_id, outcome="error", n=3):
    return [intent(state="concluded", charged=0.0, outcome=outcome, success=False, seat="a",
                   action_id=action_id) for _ in range(n)]


class NextActions(unittest.TestCase):
    def test_halted_job_does_nothing(self):
        s = snap(halted=True, seats={"lead": seat("lead", "idle", **LEAD)})
        self.assertEqual(kinds(next_actions(s, CFG)), ["nothing"])

    def test_inactive_job_does_nothing(self):
        s = snap(status="closed", seats={"lead": seat("lead", "idle", **LEAD)})
        self.assertEqual(kinds(next_actions(s, CFG)), ["nothing"])

    def test_terminal_verdict_closes(self):
        acts = next_actions(snap(goal_verdict=v("met", scope="goal")), CFG)
        self.assertEqual(kinds(acts), ["close"])

    def test_unreachable_goal_verdict_closes(self):
        self.assertEqual(kinds(next_actions(snap(goal_verdict=v("unreachable", scope="goal")), CFG)), ["close"])

    def test_gated_requested_seat_is_not_launched(self):
        s = snap(seats={"a": seat("a", "requested")}, questions={"a": [q(1)]})
        acts = next_actions(s, CFG)
        self.assertNotIn("a", [x.seat for x in acts if x.kind == "launch"])

    def test_two_failed_attempts_still_retry_the_fix(self):
        s = snap(seats={"a": seat("a", "idle")}, ledger=L(owners={"feat/a": "a"}, intents=stuck_intents(
            "gen1|launch|a|feat/a|1|fix", n=2)), handoffs=[ho(A, by="a")], verdicts={A: v("not_met", open=2)})
        self.assertEqual(named(next_actions(s, CFG)), [("launch", "a", "fix")])

    def test_seat_request_out_of_attempts_is_not_launched(self):
        s = snap(seats={"a": seat("a", "requested")}, ledger=L(intents=stuck_intents("gen1|launch|a|-|0|seat")))
        acts = next_actions(s, CFG)
        self.assertEqual(kinds(acts), ["launch"])
        self.assertTrue(acts[0].ruling)

    def test_unreceipted_verdict_is_ignored(self):
        s = snap(seats={"a": seat("a", "idle")}, ledger=L(owners={"feat/a": "a"}),
                 handoffs=[ho(A, by="a")], reviews={A: "verified"},
                 verdicts={A: v("not_met", open=2, receipt_id=None)})
        (a,) = next_actions(s, CFG)
        self.assertEqual((a.kind, a.prompt_kind), ("launch", "judge"))

    def test_requested_lead_is_launched(self):
        a = next_actions(snap(seats={"lead": seat("lead", "requested", **LEAD)}), CFG)
        self.assertEqual([(x.kind, x.seat) for x in a], [("launch", "lead")])
        self.assertEqual(a[0].action_id, "gen1|launch|lead|-|0|seat")

    def test_question_blocks_only_its_seat(self):
        s = snap(seats={"a": seat("a", "idle"), "b": seat("b", "requested")},
                 questions={"a": [q(1)]}, wakes={"a": [5]})
        self.assertEqual([x.seat for x in next_actions(s, CFG) if x.kind == "launch"], ["b"])

    def test_nothing_running_wakes_lead_and_counts_round(self):
        s = snap(seats={"lead": seat("lead", "idle", **LEAD)})
        (a,) = next_actions(s, CFG)
        self.assertEqual((a.kind, a.seat, a.reason), ("launch", "lead", "invariant-fallback"))
        self.assertIn(a.reason, NO_PROGRESS_REASONS)

    def test_three_rounds_without_progress_make_ruling_due(self):
        s = snap(ledger=L(rounds_without_progress=3), seats={"lead": seat("lead", "idle", **LEAD)})
        a = next_actions(s, CFG)
        self.assertTrue(any(x.ruling for x in a))
        self.assertEqual(a[0].reason, "no-progress")

    def test_ruling_quiesces_live_seats_first(self):
        s = snap(ledger=L(ruling_due_at=iso(T0)), live={"a"}, seats={"a": seat("a", "running")})
        self.assertEqual(kinds(next_actions(s, CFG)), ["hold"])           # within grace
        s2 = snap(ledger=L(ruling_due_at=iso(T0 - minutes(16))), live={"a"}, seats={"a": seat("a", "running")})
        self.assertEqual(kinds(next_actions(s2, CFG)), ["stop_seat"])

    def test_budget_exhausted_ruling_uses_reserve(self):
        s = snap(ledger=L(spent_usd=35.5), seats={"a": seat("a", "requested")})
        a = next_actions(s, CFG)            # 40 - 35.5 < 5 reserve + 0.5 min → no seat launch
        self.assertTrue(all(x.ruling or x.kind != "launch" for x in a))
        self.assertTrue(any(x.ruling for x in a))

    def test_not_met_resumes_owner_seat(self):
        s = snap(seats={"a": seat("a", "idle")}, ledger=L(owners={"feat/a": "a"}),
                 handoffs=[ho(A, by="a")], reviews={A: "verified"},
                 verdicts={A: v("not_met", open=2)})
        (a,) = next_actions(s, CFG)
        self.assertEqual((a.kind, a.seat, a.prompt_kind), ("launch", "a", "fix"))
        self.assertEqual(a.action_id, "gen1|launch|a|feat/a|1|fix")

    def test_released_owner_gets_successor(self):
        s = snap(seats={"a": seat("a", "released")}, ledger=L(owners={"feat/a": "a"}),
                 handoffs=[ho(A, by="a")], verdicts={A: v("not_met", open=1)},
                 reviews={A: "verified"})
        (a,) = next_actions(s, CFG)
        self.assertEqual(a.seat, "a~1")
        self.assertEqual(a.prompt_kind, "fix")

    def test_goal_ready_and_all_integrated_launches_goal_judge(self):
        s = snap(seats={}, ledger=L(owners={"feat/a": "a"}), handoffs=[ho(A, by="a")],
                 verdicts={A: v("met")}, integrated={"feat/a": {"landed": "abc"}},
                 goal_ready={"candidate": "main@abc", "at": T0})
        (a,) = next_actions(s, CFG)
        self.assertEqual((a.kind, a.prompt_kind), ("launch", "goal"))

    def test_gated_lead_holds_until_question_expiry(self):
        s = snap(seats={"lead": seat("lead", "idle", **LEAD)},
                 questions={"lead": [q(1, seat="lead")]})
        (a,) = next_actions(s, CFG)
        self.assertEqual((a.kind, a.not_before), ("hold", T0 + minutes(240)))
        self.assertNotIn(a.reason, NO_PROGRESS_REASONS)
        # a lead wake pending while gated is still a hold, not a launch
        s2 = snap(seats={"lead": seat("lead", "idle", **LEAD)},
                  questions={"lead": [q(1, seat="lead")]}, wakes={"lead": [9]})
        self.assertEqual(kinds(next_actions(s2, CFG)), ["hold"])

    def test_failed_lead_gets_one_successor_then_ruling(self):
        s1 = snap(seats={"lead": seat("lead", "failed", **LEAD)})
        self.assertEqual(named(next_actions(s1, CFG)), [("launch", "lead~1", "lead")])
        s2 = snap(seats={"lead": seat("lead", "failed", **LEAD),
                         "lead~1": seat("lead~1", "failed", **LEAD)})
        self.assertTrue(any(x.ruling for x in next_actions(s2, CFG)))

    def test_withdrawn_group_does_not_block_goal_judge(self):
        s = snap(seats={"a": seat("a", "idle")}, ledger=L(owners={"feat/a": "a", "feat/b": "a"}),
                 handoffs=[ho(A, by="a"), ho(B, by="a")], verdicts={A: v("met")},
                 integrated={"feat/a": {"landed": "abc"}, "feat/b": {"withdrawn": "abandoned"}},
                 goal_ready={"candidate": "main@abc", "at": T0})
        (a,) = next_actions(s, CFG)
        self.assertEqual((a.kind, a.prompt_kind), ("launch", "goal"))

    def test_finalize_blocked_cycle_counts_no_progress(self):
        s = snap(seats={"a": seat("a", "idle")}, ledger=L(owners={"feat/a": "a"}),
                 handoffs=[ho(A, by="a")], verdicts={A: v("met")},
                 integrated={"feat/a": {"blocked": "CI red on abc"}})
        (a,) = next_actions(s, CFG)
        self.assertEqual((a.kind, a.seat, a.prompt_kind), ("launch", "a", "fix"))
        self.assertEqual(a.reason, "finalize-blocked")
        self.assertIn(a.reason, NO_PROGRESS_REASONS)

    def test_max_seats_refusal_wake_counts_no_progress(self):
        seats = {f"s{i}": seat(f"s{i}", "idle") for i in range(11)}
        seats.update(lead=seat("lead", "idle", **LEAD), x=seat("x", "requested"))   # 13 > max_seats 12
        (a,) = next_actions(snap(seats=seats), CFG)
        self.assertEqual((a.kind, a.seat, a.reason), ("launch", "lead", "max-seats-refused"))
        self.assertIn(a.reason, NO_PROGRESS_REASONS)

    def test_max_seats_refused(self):
        seats = {f"s{i}": seat(f"s{i}", "idle") for i in range(11)}
        seats.update(lead=seat("lead", "idle", **LEAD), x=seat("x", "requested"))
        acts = next_actions(snap(seats=seats), CFG)
        self.assertNotIn("x", [x.seat for x in acts if x.kind == "launch"])
        self.assertIn(("lead", "max-seats-refused"), [(x.seat, x.reason) for x in acts])

    def test_nothing_else_to_do_definition(self):
        # every pending action is out of attempts, no seat is live or wakeable: a ruling, nothing else
        s = snap(seats={"a": seat("a", "idle")}, ledger=L(owners={"feat/a": "a"}, intents=stuck_intents(
            "gen1|launch|a|feat/a|1|fix")), handoffs=[ho(A, by="a")], verdicts={A: v("not_met", open=2)})
        acts = next_actions(s, CFG)
        self.assertEqual(kinds(acts), ["launch"])
        self.assertTrue(acts[0].ruling)
        self.assertIsNone(acts[0].seat)

    def test_outage_attempt_not_counted(self):
        s = snap(seats={"a": seat("a", "idle")}, ledger=L(owners={"feat/a": "a"}, intents=stuck_intents(
            "gen1|launch|a|feat/a|1|fix", outcome="outage")), handoffs=[ho(A, by="a")],
            verdicts={A: v("not_met", open=2)})
        self.assertEqual(named(next_actions(s, CFG)), [("launch", "a", "fix")])

    def test_question_to_role_wakes_that_seat(self):
        s = snap(seats={"lead": seat("lead", "idle", **LEAD)}, wakes={"lead": [7]})
        self.assertEqual(named(next_actions(s, CFG)), [("launch", "lead", "wake")])

    def test_land_lease_hold_over_limit_makes_ruling_due(self):
        s = snap(seats={"lead": seat("lead", "idle", **LEAD)}, handoffs=[ho(A, by="a")],
                 verdicts={A: v("met")},
                 integrated={"feat/a": {"lease_held_since": iso(T0 - minutes(61))}})
        self.assertTrue(any(x.ruling for x in next_actions(s, CFG)))

    def test_driver_error_passes_make_ruling_due(self):
        s = snap(seats={"lead": seat("lead", "idle", **LEAD)}, ledger=L(errors=6))
        a = next_actions(s, CFG)
        self.assertTrue(any(x.ruling for x in a))
        self.assertEqual(a[0].reason, "driver-errors")

    def test_stall_minutes_wakes_lead(self):
        s = snap(seats={"a": seat("a", "running", updated_at=T0 - minutes(91)),
                        "lead": seat("lead", "idle", updated_at=T0 - minutes(91), **LEAD)},
                 live={"a"})
        (a,) = next_actions(s, CFG)
        self.assertEqual((a.kind, a.seat, a.reason), ("launch", "lead", "stall"))
        self.assertIn(a.reason, NO_PROGRESS_REASONS)

    def test_artifact_recipe_goal_judge_after_all_groups_met(self):
        base = dict(seats={"a": seat("a", "idle")}, ledger=L(owners={"report:x": "a"}),
                    handoffs=[ho(R, by="a")], goal_ready={"candidate": "report:x@" + "c" * 40, "at": T0})
        met = next_actions(snap(verdicts={R: v("met")}, **base), ARTIFEXT_CFG)
        self.assertEqual(named(met), [("launch", None, "goal")])
        not_met = next_actions(snap(verdicts={R: v("not_met", open=1)}, **base), ARTIFEXT_CFG)
        self.assertEqual(named(not_met), [("launch", "a", "fix")])

    def test_unreviewed_handoff_goes_to_reviewer(self):
        s = snap(seats={"lead": seat("lead", "idle", **LEAD)}, handoffs=[ho(A, by="a")])
        self.assertEqual(named(next_actions(s, CFG)), [("launch", None, "review")])

    def test_changes_review_resumes_owner(self):
        s = snap(seats={"a": seat("a", "idle")}, ledger=L(owners={"feat/a": "a"}),
                 handoffs=[ho(A, by="a")], reviews={A: "changes"})
        (a,) = next_actions(s, CFG)
        self.assertEqual((a.seat, a.prompt_kind, a.reason), ("a", "fix", "changes"))

    def test_met_hand_off_not_integrated_launches_integrator(self):
        s = snap(seats={}, handoffs=[ho(A, by="a")], verdicts={A: v("met")})
        self.assertEqual(named(next_actions(s, CFG)), [("launch", None, "integrate")])

    def test_ruling_in_flight_is_responsible(self):
        s = snap(ledger=L(rounds_without_progress=3,
                          intents=[intent(state="claimed", seat=None, action_id="gen1|launch|-|ruling|0|ruling")]),
                 seats={"lead": seat("lead", "idle", **LEAD)})
        (a,) = next_actions(s, CFG)
        self.assertEqual((a.kind, a.responsible), ("nothing", "ruling"))

    def test_ruling_attempts_exhausted_records_unreachable(self):
        s = snap(ledger=L(ruling_due_at=iso(T0 - minutes(1)), ruling_attempts=2),
                 seats={"lead": seat("lead", "idle", **LEAD)})
        self.assertEqual(kinds(next_actions(s, CFG)), ["record_unreachable"])

    def test_ruling_deadline_records_unreachable(self):
        s = snap(ledger=L(ruling_due_at=iso(T0 - minutes(121))), seats={"lead": seat("lead", "idle", **LEAD)})
        self.assertEqual(kinds(next_actions(s, CFG)), ["record_unreachable"])

    def test_reserve_gone_records_unreachable(self):
        s = snap(ledger=L(spent_usd=40), seats={"lead": seat("lead", "idle", **LEAD)})
        self.assertEqual(kinds(next_actions(s, CFG)), ["record_unreachable"])

    def test_stuck_running_seat_is_relaunched(self):
        s = snap(seats={"a": seat("a", "running")})
        self.assertEqual(named(next_actions(s, CFG)), [("launch", "a", "relaunch")])
        self.assertEqual(next_actions(s, CFG)[0].reason, "stall")

    def test_open_judge_is_responsible_not_fallback(self):
        s = snap(handoffs=[ho(A, by="a")], reviews={A: "verified"},
                 ledger=L(intents=[intent(state="claimed", seat=None,
                                          action_id="gen1|launch|-|feat/a|1|judge")]))
        (a,) = next_actions(s, CFG)
        self.assertEqual((a.kind, a.responsible), ("nothing", "runner"))

    def test_never_empty_for_active_job(self):
        lead = seat("lead", "idle", **LEAD)
        cases = [
            snap(seats={"lead": lead}),
            snap(seats={"lead": seat("lead", "running", **LEAD)}, live={"lead"}),
            snap(seats={"a": seat("a", "running")}, live={"a"}),
            snap(seats={"lead": seat("lead", "released", **LEAD)}),
            snap(seats={}),
        ]
        for s in cases:
            self.assertTrue(next_actions(s, CFG), s)

    def test_action_ids_are_unique_per_logical_action(self):
        s = snap(seats={"a": seat("a", "idle"), "b": seat("b", "idle")}, wakes={"a": [3], "b": [4]})
        ids = [x.action_id for x in next_actions(s, CFG)]
        self.assertEqual(len(ids), len(set(ids)))


class Progress(unittest.TestCase):
    def test_first_judged_round_is_no_progress(self):
        self.assertFalse(progress(None, {"artifact": "x", "open": 1}))

    def test_new_artifact_and_open_decreased_is_progress(self):
        self.assertTrue(progress({"artifact": "a", "open": 3}, {"artifact": "b", "open": 2}))

    def test_same_artifact_is_not_progress(self):
        self.assertFalse(progress({"artifact": "a", "open": 3}, {"artifact": "a", "open": 2}))

    def test_open_not_decreased_is_not_progress(self):
        self.assertFalse(progress({"artifact": "a", "open": 3}, {"artifact": "b", "open": 3}))

    def test_judge_progress_yes_is_progress(self):
        self.assertTrue(progress({"artifact": "a", "open": 3}, {"artifact": "a", "open": 3, "progress": "yes"}))

    def test_integration_is_progress(self):
        self.assertTrue(progress({"artifact": "a", "open": 3}, {"integrated": True}))

    def test_goal_open_decrease_is_progress_on_the_same_target(self):
        self.assertTrue(progress({"artifact": "t", "open": 3, "scope": "goal"},
                                 {"artifact": "t", "open": 2, "scope": "goal"}))


class Reasons(unittest.TestCase):
    def test_action_kinds_are_the_frozen_set(self):
        self.assertEqual(set(dm.ACTION_KINDS), {"launch", "record_unreachable", "close", "hold", "nothing",
                                                 "rollover", "stop_seat", "complete_land"})


if __name__ == "__main__":
    unittest.main()
