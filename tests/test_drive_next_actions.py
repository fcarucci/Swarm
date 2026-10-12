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


def stuck_intents(action_id, outcome="error", n=3, success=False):
    return [intent(state="concluded", charged=0.0, outcome=outcome, success=success, seat="a",
                   action_id=action_id) for _ in range(n)]


class NextActions(unittest.TestCase):
    def test_halted_job_does_nothing(self):
        s = snap(halted=True, seats={"lead": seat("lead", "idle", **LEAD)})
        self.assertEqual(kinds(next_actions(s, CFG)), ["nothing"])

    def test_inactive_job_does_nothing(self):
        s = snap(status="closed", seats={"lead": seat("lead", "idle", **LEAD)})
        self.assertEqual(kinds(next_actions(s, CFG)), ["nothing"])

    def test_terminal_verdict_closes(self):
        acts = next_actions(snap(goal_verdict=v("met", scope="goal", candidate="main@" + "a" * 40),
                                 integrated={"feat/a": {"landed": "a" * 40}}), CFG)
        self.assertEqual(kinds(acts), ["close"])

    def test_unreachable_goal_verdict_closes(self):
        self.assertEqual(kinds(next_actions(snap(goal_verdict=v("unreachable", scope="goal")), CFG)), ["close"])

    def test_gated_requested_seat_is_not_launched(self):
        s = snap(seats={"a": seat("a", "requested")}, questions={"a": [q(1)]})
        acts = next_actions(s, CFG)
        self.assertNotIn("a", [x.seat for x in acts if x.kind == "launch"])

    def test_two_failed_attempts_still_retry_the_fix(self):
        s = snap(seats={"a": seat("a", "idle")}, ledger=L(owners={"feat/a": "a"}, intents=stuck_intents(
            "gen1|fix|a|feat/a|1|1", n=2)), handoffs=[ho(A, by="a")], verdicts={A: v("not_met", open=2)})
        self.assertEqual(named(next_actions(s, CFG)), [("launch", "a", "fix")])

    def test_seat_request_out_of_attempts_is_not_launched(self):
        s = snap(seats={"a": seat("a", "requested")}, ledger=L(intents=stuck_intents("gen1|seat|a|-|0|1")))
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
        self.assertEqual(a[0].action_id, "gen1|seat|lead|-|0|1")

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
        self.assertEqual(a.action_id, "gen1|fix|a|feat/a|1|1")

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
                 goal_ready={"candidate": "main@abc", "req": "r1", "at": T0})
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
                 goal_ready={"candidate": "main@abc", "req": "r1", "at": T0})
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
            "gen1|fix|a|feat/a|1|1")), handoffs=[ho(A, by="a")], verdicts={A: v("not_met", open=2)})
        acts = next_actions(s, CFG)
        self.assertEqual(kinds(acts), ["launch"])
        self.assertTrue(acts[0].ruling)
        self.assertIsNone(acts[0].seat)

    def test_outage_attempt_not_counted(self):
        s = snap(seats={"a": seat("a", "idle")}, ledger=L(owners={"feat/a": "a"}, intents=stuck_intents(
            "gen1|fix|a|feat/a|1|1", outcome="outage")), handoffs=[ho(A, by="a")],
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
                    handoffs=[ho(R, by="a")], goal_ready={"candidate": "report:x@" + "c" * 40, "req": "r1", "at": T0})
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
                          intents=[intent(state="claimed", seat=None, action_id="gen1|ruling|-|ruling|0|unrecorded")]),
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
                                          action_id="gen1|judge|-|feat/a|1|1")]))
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


class TriggerIds(unittest.TestCase):
    """Action ids are `gen<N>|<prompt kind>|<seat>|<group>|<round>|<trigger>` (plan 88854be): two distinct
    triggers of one action get distinct ids; a retry of the same trigger reuses its id."""

    def test_seat_request_id_follows_the_seat_revision(self):
        r1 = next_actions(snap(seats={"a": seat("a", "requested", revision=1)}), CFG)[0]
        r2 = next_actions(snap(seats={"a": seat("a", "requested", revision=2)}), CFG)[0]
        self.assertEqual(r1.action_id, "gen1|seat|a|-|0|1")
        self.assertNotEqual(r1.action_id, r2.action_id)

    def test_judge_id_follows_the_hand_off_receipt(self):
        def judge(receipt):
            s = snap(seats={"lead": seat("lead", "idle", **LEAD)}, handoffs=[ho(A, by="a", receipt_id=receipt)],
                     reviews={A: "verified"})
            return next_actions(s, CFG)[0].action_id
        self.assertEqual(judge(1), "gen1|judge|-|feat/a|1|1")
        self.assertNotEqual(judge(1), judge(2))

    def test_fallback_id_follows_rounds_without_progress(self):
        def fallback(rounds):
            s = snap(seats={"lead": seat("lead", "idle", **LEAD)}, ledger=L(rounds_without_progress=rounds))
            return next_actions(s, CFG)[0].action_id
        self.assertEqual(fallback(0), "gen1|fallback|lead|-|0|0")
        self.assertNotEqual(fallback(0), fallback(1))

    def test_ruling_id_follows_ruling_due_at(self):
        def ruling(due):
            s = snap(seats={"lead": seat("lead", "idle", **LEAD)}, ledger=L(ruling_due_at=due))
            return next_actions(s, CFG)[0].action_id
        self.assertNotEqual(ruling(iso(T0)), ruling(iso(T0 + minutes(1))))

    def test_relaunch_id_follows_the_lost_launch(self):
        def relaunch(launch_id):
            s = snap(seats={"a": seat("a", "running")},
                     ledger=L(intents=[intent(state="concluded", charged=0.0, outcome="error", success=False,
                                              seat="a", launch_id=launch_id)]))
            return next_actions(s, CFG)[0].action_id
        self.assertNotEqual(relaunch("L1"), relaunch("L2"))

    def test_retry_of_the_same_trigger_reuses_its_id(self):
        s = snap(seats={"a": seat("a", "requested", revision=1)})
        self.assertEqual(next_actions(s, CFG)[0].action_id, next_actions(s, CFG)[0].action_id)
        retried = snap(seats={"a": seat("a", "requested", revision=1)},
                       ledger=L(intents=stuck_intents("gen1|seat|a|-|0|1", n=2)))
        (a,) = next_actions(retried, CFG)
        self.assertEqual((a.kind, a.action_id), ("launch", "gen1|seat|a|-|0|1"))

    def test_successful_repeats_do_not_cap_a_new_trigger(self):
        s = snap(seats={"a": seat("a", "idle")}, ledger=L(owners={"feat/a": "a"}, intents=stuck_intents(
            "gen1|fix|a|feat/a|1|1", outcome="completed", n=3, success=True)),
            handoffs=[ho(A, by="a")], verdicts={A: v("not_met", open=2, receipt_id=2)})
        (a,) = next_actions(s, CFG)
        self.assertEqual((a.kind, a.action_id), ("launch", "gen1|fix|a|feat/a|1|2"))

    def test_lead_wake_uses_the_smallest_pending_event(self):
        (a,) = next_actions(snap(seats={"lead": seat("lead", "idle", **LEAD)}, wakes={"lead": [8, 4]}), CFG)
        self.assertEqual(a.action_id, "gen1|wake|lead|-|0|4")

    def test_seat_wake_uses_the_smallest_pending_event(self):
        (a,) = next_actions(snap(seats={"a": seat("a", "idle")}, wakes={"a": [9, 3]}), CFG)
        self.assertEqual(a.action_id, "gen1|wake|a|-|0|3")

    def test_goal_id_follows_the_goal_ready_record(self):
        s = snap(seats={}, ledger=L(owners={"feat/a": "a"}), handoffs=[ho(A, by="a")], verdicts={A: v("met")},
                 integrated={"feat/a": {"landed": "abc"}},
                 goal_ready={"id": 77, "candidate": "main@abc", "req": "r1", "at": T0})
        (a,) = next_actions(s, CFG)
        self.assertEqual((a.prompt_kind, a.action_id), ("goal", "gen1|goal|-|goal|0|77"))


class GoalNeedsLandedWork(unittest.TestCase):
    """E-land on int/rc 32d21ea6 (QA's MET run): an engineer's DONE never receipted left no hand-off group, and
    "no groups" read as "all integrated" launched the goal judge on an unlanded branch."""
    GR = {"id": 3, "candidate": "main@abc", "req": "r1", "at": T0}

    def test_coding_job_with_no_groups_wakes_the_lead_not_the_goal_judge(self):
        s = snap(seats={"lead": seat("lead", "idle", **LEAD)}, goal_ready=self.GR)
        acts = next_actions(s, CFG)
        self.assertNotIn("goal", [x.prompt_kind for x in acts])
        self.assertEqual(named(acts), [("launch", "lead", "fallback")])

    def test_coding_job_whose_only_group_is_withdrawn_is_not_ready(self):
        s = snap(seats={"lead": seat("lead", "idle", **LEAD)}, ledger=L(owners={"feat/a": "a"}),
                 handoffs=[ho(A, by="a")], integrated={"feat/a": {"withdrawn": "abandoned"}}, goal_ready=self.GR)
        self.assertNotIn("goal", [x.prompt_kind for x in next_actions(s, CFG)])

    def test_coding_job_with_an_unlanded_group_is_not_judged(self):
        s = snap(seats={"lead": seat("lead", "idle", **LEAD)}, ledger=L(owners={"feat/a": "a", "feat/b": "a"}),
                 handoffs=[ho(A, by="a"), ho(B, by="a")], verdicts={A: v("met"), B: v("met")},
                 integrated={"feat/a": {"landed": "abc"}}, goal_ready=self.GR)
        self.assertNotIn("goal", [x.prompt_kind for x in next_actions(s, CFG)])

    def test_coding_job_launches_the_goal_judge_once_every_group_is_integrated(self):
        s = snap(seats={"lead": seat("lead", "idle", **LEAD)}, ledger=L(owners={"feat/a": "a", "feat/b": "a"}),
                 handoffs=[ho(A, by="a"), ho(B, by="a")], verdicts={A: v("met"), B: v("met")},
                 integrated={"feat/a": {"landed": "abc"}, "feat/b": {"landed": "def"}}, goal_ready=self.GR)
        self.assertEqual(named(next_actions(s, CFG)), [("launch", None, "goal")])

    def test_artifact_job_with_no_groups_is_unaffected(self):
        s = snap(seats={"lead": seat("lead", "idle", **LEAD)},
                 goal_ready={"id": 3, "candidate": R, "req": "r1", "at": T0})
        self.assertEqual(named(next_actions(s, ARTIFEXT_CFG)), [("launch", None, "goal")])


class MetNeedsTheCandidateLanded(unittest.TestCase):
    """Final judge on int/rc-next (QA live-met 20261012T132049): a ruling MET closed a coding job with nothing
    landed (origin and backup main still at the initial commit; no reviewer, integrator or INTEGRATED)."""
    CAND = "c" * 40

    def test_ruling_met_on_an_unlanded_candidate_is_recorded_unreachable_not_closed(self):
        s = snap(goal_verdict=v("met", scope="ruling", candidate=self.CAND))
        (a,) = next_actions(s, CFG)
        self.assertEqual((a.kind, a.ruling), ("record_unreachable", True))
        self.assertIn("not landed", a.reason)

    def test_goal_met_on_an_unlanded_candidate_does_not_close(self):
        s = snap(seats={"lead": seat("lead", "idle", **LEAD)}, goal_verdict=v("met", scope="goal", candidate=self.CAND),
                 integrated={"feat/a": {"landed": "d" * 40}})           # another sha landed: not the candidate
        self.assertNotIn("close", kinds(next_actions(s, CFG)))

    def test_met_closes_once_the_candidate_is_the_landed_head(self):
        for scope in ("goal", "ruling"):
            s = snap(goal_verdict=v("met", scope=scope, candidate="main@" + self.CAND),
                     integrated={"feat/a": {"landed": self.CAND}})
            self.assertEqual(kinds(next_actions(s, CFG)), ["close"], scope)

    def test_artifact_job_and_unreachable_are_unaffected(self):
        self.assertEqual(kinds(next_actions(snap(goal_verdict=v("met", scope="ruling", candidate=R)), ARTIFEXT_CFG)),
                         ["close"])
        self.assertEqual(kinds(next_actions(snap(goal_verdict=v("unreachable", scope="ruling")), CFG)), ["close"])

    def test_candidate_landed_needs_a_real_sha(self):
        self.assertFalse(dm.candidate_landed({"g": {"landed": None}}, self.CAND))
        self.assertFalse(dm.candidate_landed({"g": {"landed": self.CAND}}, None))
        self.assertTrue(dm.candidate_landed({"g": {"landed": self.CAND}}, "main@" + self.CAND[:12]))


class SurvivorKills(unittest.TestCase):
    """One test per mutant the review (task7-next.md) found surviving the first gate."""

    def test_ruling_grace_boundary_stops_at_exactly_grace(self):          # M1
        s = snap(ledger=L(ruling_due_at=iso(T0 - minutes(15))), live={"a"}, seats={"a": seat("a", "running")})
        self.assertEqual(kinds(next_actions(s, CFG)), ["stop_seat"])

    def test_question_until_is_capped_at_question_expiry(self):          # M2
        s = snap(seats={"lead": seat("lead", "idle", **LEAD)},
                 questions={"lead": [q(1, seat="lead", until=T0 + minutes(600))]})
        (a,) = next_actions(s, CFG)
        self.assertEqual((a.kind, a.not_before), ("hold", T0 + minutes(240)))

    def test_concurrency_cap_limits_seat_launches(self):                 # M12
        cfg = {**CFG, "max_concurrent_sessions": 2}
        s = snap(seats={"x": seat("x", "running"), "a": seat("a", "requested"), "b": seat("b", "requested")},
                 live={"x"})
        self.assertEqual([x.seat for x in next_actions(s, cfg) if x.kind == "launch"], ["a"])

    def test_max_seats_boundary_admits_the_twelfth_seat(self):           # M13
        seats = {f"s{i}": seat(f"s{i}", "idle") for i in range(11)}
        seats["x"] = seat("x", "requested")                                # 11 others + x = 12 seats
        self.assertEqual([x.seat for x in next_actions(snap(seats=seats), CFG) if x.kind == "launch"], ["x"])

    def test_max_seats_counts_dead_seats(self):
        seats = {f"d{i}": seat(f"d{i}", "released") for i in range(12)}
        seats["x"] = seat("x", "requested")
        acts = next_actions(snap(seats=seats), CFG)
        self.assertNotIn("x", [x.seat for x in acts if x.kind == "launch"])

    def test_wall_clock_ruling_at_max_wall_hours(self):                  # M16
        s = snap(ledger=L(activated_at=iso(T0 - minutes(25 * 60))), seats={"lead": seat("lead", "idle", **LEAD)})
        a = next_actions(s, CFG)
        self.assertTrue(any(x.ruling for x in a))
        self.assertEqual(a[0].reason, "wall-clock")

    def test_artifact_recipe_unreachable_artifact_does_not_gate_goal(self):  # M17
        s = snap(seats={"a": seat("a", "idle")}, handoffs=[ho(R, by="a")], verdicts={R: v("unreachable")},
                 goal_ready={"id": 5, "candidate": "report:x@" + "c" * 40, "req": "r", "at": T0})
        self.assertNotIn("goal", [x.prompt_kind for x in next_actions(s, ARTIFEXT_CFG)])

    def test_stop_seat_is_a_ruling_action(self):                         # M18
        s = snap(ledger=L(ruling_due_at=iso(T0 - minutes(16))), live={"a"}, seats={"a": seat("a", "running")})
        (a,) = next_actions(s, CFG)
        self.assertEqual((a.kind, a.ruling), ("stop_seat", True))

    def test_stale_generation_terminal_verdict_does_not_close(self):     # M20
        acts = next_actions(snap(goal_verdict=v("met", scope="goal", generation=0)), CFG)
        self.assertNotIn("close", kinds(acts))

    def test_gated_owner_holds_the_job_not_the_lead(self):              # F1
        s = snap(seats={"a": seat("a", "idle"), "lead": seat("lead", "idle", **LEAD)},
                 ledger=L(owners={"feat/a": "a"}), handoffs=[ho(A, by="a")], reviews={A: "verified"},
                 verdicts={A: v("not_met", open=2)}, questions={"a": [q(1)]})
        (a,) = next_actions(s, CFG)
        self.assertEqual((a.kind, a.not_before), ("hold", T0 + minutes(240)))

    def test_gated_requested_seat_does_not_wake_the_lead(self):          # F1
        s = snap(seats={"b": seat("b", "requested"), "lead": seat("lead", "idle", **LEAD)},
                 questions={"b": [q(1, seat="b")]})
        self.assertEqual(kinds(next_actions(s, CFG)), ["hold"])

    def test_goal_rejudged_after_newer_string_goal_ready(self):          # finding 7
        s = snap(goal_verdict=v("not_met", scope="goal", at=T0), ledger=L(owners={"feat/a": "a"}),
                 handoffs=[ho(A, by="a")], verdicts={A: v("met")}, integrated={"feat/a": {"landed": "abc"}},
                 goal_ready={"id": 9, "candidate": "m@1", "req": "r", "at": iso(T0 + minutes(5))})
        self.assertEqual(named(next_actions(s, CFG)), [("launch", None, "goal")])

    def test_goal_needs_the_pinned_requirements(self):                   # finding 5
        s = snap(seats={}, ledger=L(owners={"feat/a": "a"}), handoffs=[ho(A, by="a")], verdicts={A: v("met")},
                 integrated={"feat/a": {"landed": "abc"}}, goal_ready={"id": 1, "candidate": "main@abc"})
        self.assertNotIn("goal", [x.prompt_kind for x in next_actions(s, CFG)])

    def test_ruling_without_stamp_holds_from_now(self):                  # finding 6 (caller contract)
        s = snap(ledger=L(rounds_without_progress=3), live={"a"}, seats={"a": seat("a", "running")})
        (a,) = next_actions(s, CFG)
        self.assertEqual((a.kind, a.not_before), ("hold", T0 + minutes(15)))


class Progress(unittest.TestCase):
    """progress(prev, new) takes judge VerdictViews and the board's bool flag (plan eceb35e)."""

    def test_first_judged_round_is_no_progress(self):
        self.assertFalse(progress(None, v("not_met", artifact="x", open=1)))

    def test_new_artifact_and_open_decreased_is_progress(self):
        self.assertTrue(progress(v("not_met", artifact="a", open=3), v("not_met", artifact="b", open=2)))

    def test_same_artifact_is_not_progress(self):
        self.assertFalse(progress(v("not_met", artifact="a", open=3), v("not_met", artifact="a", open=2)))

    def test_open_not_decreased_is_not_progress(self):
        self.assertFalse(progress(v("not_met", artifact="a", open=3), v("not_met", artifact="b", open=3)))

    def test_judge_progress_flag_is_progress(self):
        self.assertTrue(progress(v("not_met", artifact="a", open=3),
                                 v("not_met", artifact="a", open=3, progress=True)))

    def test_judge_progress_flag_false_is_not_progress_by_itself(self):
        self.assertFalse(progress(v("not_met", artifact="a", open=3),
                                  v("not_met", artifact="a", open=3, progress=False)))

    def test_integration_is_progress(self):
        self.assertTrue(progress(v("not_met", artifact="a", open=3), v("not_met", artifact="a", open=3),
                                 integrated=True))

    def test_goal_open_decrease_is_progress_on_the_same_target(self):
        self.assertTrue(progress(v("not_met", scope="goal", artifact="t", open=3),
                                 v("not_met", scope="goal", artifact="t", open=2)))


class Reasons(unittest.TestCase):
    def test_action_kinds_are_the_frozen_set(self):
        self.assertEqual(set(dm.ACTION_KINDS), {"launch", "record_unreachable", "close", "hold", "nothing",
                                                 "rollover", "stop_seat", "complete_land"})


class MaxRounds(unittest.TestCase):
    """spec 4.1 and 3.6: max_rounds (8 judged rounds per artifact group) is an absolute cap that makes a
    ruling due. It is per group, not a total over the job."""

    def lead_idle(self):
        return {"lead": seat("lead", "idle", **LEAD)}

    def test_group_at_max_rounds_makes_ruling_due(self):
        acts = next_actions(snap(rounds={"feat/a": 8}, seats=self.lead_idle()), CFG)
        self.assertEqual(kinds(acts), ["launch"])
        self.assertTrue(acts[0].ruling)
        self.assertEqual(acts[0].prompt_kind, "ruling")

    def test_group_above_max_rounds_makes_ruling_due(self):
        acts = next_actions(snap(rounds={"feat/a": 9}, seats=self.lead_idle()), CFG)
        self.assertTrue(acts[0].ruling)

    def test_one_round_below_max_is_not_a_ruling(self):
        acts = next_actions(snap(rounds={"feat/a": 7}, seats=self.lead_idle()), CFG)
        self.assertFalse(any(x.ruling for x in acts))
        self.assertEqual(named(acts), [("launch", "lead", "fallback")])

    def test_rounds_are_counted_per_group_not_summed(self):
        acts = next_actions(snap(rounds={"feat/a": 4, "feat/b": 4}, seats=self.lead_idle()), CFG)
        self.assertFalse(any(x.ruling for x in acts))

    def test_any_one_group_at_max_rounds_makes_ruling_due(self):
        acts = next_actions(snap(rounds={"feat/a": 1, "feat/b": 8}, seats=self.lead_idle()), CFG)
        self.assertTrue(acts[0].ruling)

    def test_max_rounds_is_configurable(self):
        acts = next_actions(snap(rounds={"feat/a": 3}, seats=self.lead_idle()), {**CFG, "max_rounds": 3})
        self.assertTrue(acts[0].ruling)

    def test_ruling_for_max_rounds_names_its_cause(self):
        acts = next_actions(snap(rounds={"feat/a": 8}, seats=self.lead_idle()), CFG)
        self.assertEqual(acts[0].reason, "max-rounds")


class StallWakeTrigger(unittest.TestCase):
    """spec 3.9 and the action-id rule: a stall wake's trigger is the ISO time of the last progress it reacts
    to (the newest seat, hand-off or verdict activity; the ledger keeps no separate progress stamp)."""

    def stalled(self, last, now=T0):
        return snap(now=now, live={"a"}, seats={
            "lead": seat("lead", "idle", updated_at=last, **LEAD),
            "a": seat("a", "running", updated_at=last),
        })

    def test_stall_wake_trigger_is_the_time_of_last_progress(self):
        last = T0 - minutes(100)
        acts = next_actions(self.stalled(last), CFG)
        self.assertEqual(named(acts), [("launch", "lead", "stall")])
        self.assertEqual(acts[0].action_id, f"gen1|stall|lead|-|0|{iso(last)}")

    def test_stall_wake_waits_for_stall_minutes(self):
        last = T0 - minutes(89)
        acts = next_actions(self.stalled(last), CFG)
        self.assertEqual(kinds(acts), ["nothing"])
        self.assertEqual(acts[0].responsible, "a")

    def test_stall_wake_fires_at_exactly_stall_minutes(self):
        last = T0 - minutes(90)
        self.assertEqual(named(next_actions(self.stalled(last), CFG)), [("launch", "lead", "stall")])

    def test_newest_activity_resets_the_stall_clock(self):
        s = snap(now=T0, live={"a"}, seats={
            "lead": seat("lead", "idle", updated_at=T0 - minutes(300), **LEAD),
            "a": seat("a", "running", updated_at=T0 - minutes(300)),
        }, verdicts={B: v("not_met", artifact=B, open=3, at=T0 - minutes(10))})
        self.assertEqual(kinds(next_actions(s, CFG)), ["nothing"])


if __name__ == "__main__":
    unittest.main()
