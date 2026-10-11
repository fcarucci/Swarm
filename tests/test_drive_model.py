"""The frozen drive-driver model (spec 3.2-3.4, plan Task 7a): exact field lists, frozen and slotted
dataclasses, and the factories in drive_fixtures that Tasks 7-8c build on."""
from __future__ import annotations

import dataclasses
import datetime as dt
import typing
import unittest

import support  # noqa: F401  (puts lib/ on sys.path)
from drive_fixtures import L, T0, Intent, Receipt, ho, intent, iso, minutes, q, receipt, seat, snap, v
from swarm.supervisor import driver_model as dm

# Pinned against eng-ledger at 534cfce. If these change, update drive_fixtures.py to match.
# Field names of the Ledger dataclass (lib/swarm/supervisor/ledger.py @ 534cfce): 25 fields.
LEDGER_FIELDS = (
    "board_key", "job", "generation", "generation_requested", "activated_at", "budgets", "intents",
    "owners", "integrated", "ruling_due_at", "ruling_attempts", "rounds_without_progress", "last_progress_at", "errors",
    "closing", "halted", "terminal", "mirror_fetch_pending", "last_pass", "watermarks",
    "terminal_intent", "halted_total_seconds", "requester_session", "pinned_requirements",
    "incarnation", "ledger_rev", "state", "models",
)
assert len(LEDGER_FIELDS) == 27

# The Intent dataclass (lib/swarm/supervisor/ledger.py @ 534cfce): 11 fields.
INTENT_FIELDS = (
    "launch_id", "action_id", "attempt", "seat", "reserved_usd", "reserved_minutes", "locks", "state",
    "outcome", "success", "charged_usd",
)
assert len(INTENT_FIELDS) == 11

# The Receipt dataclass (lib/swarm/receipts.py @ 534cfce): 14 fields.
RECEIPT_FIELDS = (
    "board_key", "job", "generation", "incarnation", "action_id", "attempt", "seat", "kind", "record_id",
    "rev", "content_sha", "args_sha", "expires_at", "destinations",
)
assert len(RECEIPT_FIELDS) == 14

# Receipt kinds: the real list, never a hand-written copy (it drifted once).
from swarm.receipts import KINDS as RECEIPT_KINDS  # noqa: E402

SNAPSHOT_FIELDS = (
    "job", "generation", "goal", "status", "halted", "ledger", "seats", "live", "handoffs", "reviews",
    "verdicts", "goal_verdict", "goal_ready", "questions", "wakes", "integrated", "rounds", "now",
)

ACTION_FIELDS = (
    "kind", "action_id", "seat", "prompt_kind", "artifact", "ruling", "reason", "not_before", "responsible",
)

# spec 3.4 `seats` table, schema 25
SEAT_FIELDS = (
    "id", "job", "seat", "role", "title", "harness", "model", "workdir", "brief_path", "task_ref",
    "complexity", "requested_by", "requested_at", "state", "agent_key", "session_id", "generation",
    "revision", "launches", "last_outcome", "wake_reason", "wake_watermark", "updated_at",
)

# verdict fields the review.verdict_data record carries (plan line 198) must be on VerdictView
VERDICT_DATA_KEYS = ("scope", "generation", "open", "progress", "candidate", "req")

ACTION_KINDS = (
    "launch", "record_unreachable", "close", "hold", "nothing", "rollover", "stop_seat", "complete_land",
)


class FrozenModelTest(unittest.TestCase):
    def test_snapshot_is_frozen(self):
        s = snap()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            s.halted = True
        changed = dataclasses.replace(s, halted=True)
        self.assertTrue(changed.halted)
        self.assertFalse(s.halted)
        self.assertIsNot(changed, s)

    def test_factories_cover_every_field(self):
        # Each factory must accept an override for every field of the type it builds, and keep it.
        cases = (
            (dm.Snapshot, snap), (dm.SeatView, seat), (dm.Question, q),
            (dm.HandoffView, ho), (dm.VerdictView, v), (L, L), (Receipt, receipt), (Intent, intent),
        )
        for cls, factory in cases:
            for f in dataclasses.fields(cls):
                with self.subTest(cls=cls.__name__, field=f.name):
                    sentinel = object()
                    built = factory(**{f.name: sentinel})
                    self.assertIs(getattr(built, f.name), sentinel)

    def test_snapshot_field_list_is_the_frozen_interface(self):
        self.assertEqual(tuple(f.name for f in dataclasses.fields(dm.Snapshot)), SNAPSHOT_FIELDS)

    def test_action_field_list_and_defaults(self):
        self.assertEqual(tuple(f.name for f in dataclasses.fields(dm.Action)), ACTION_FIELDS)
        a = dm.Action("launch", "gen1|launch|a|-|1|-", "a", "fix")
        self.assertEqual((a.artifact, a.ruling, a.reason, a.not_before, a.responsible),
                         (None, False, "", None, None))

    def test_action_kinds_are_the_frozen_set(self):
        self.assertEqual(dm.ACTION_KINDS, ACTION_KINDS)
        for kind in ACTION_KINDS:
            with self.subTest(kind=kind):
                a = dm.Action(kind, "id", None, None, not_before=T0, responsible="runner")
                self.assertEqual((a.kind, a.not_before, a.responsible), (kind, T0, "runner"))

    def test_seat_view_field_list_matches_spec_seats_table(self):
        self.assertEqual(tuple(f.name for f in dataclasses.fields(dm.SeatView)), SEAT_FIELDS)

    def test_seat_view_is_slotted_and_frozen(self):
        s = seat("a", "idle")
        self.assertFalse(hasattr(s, "__dict__"))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            s.state = "running"
        # An undeclared attribute must not be settable (no __dict__). CPython 3.13 raises TypeError here
        # from the generated frozen __setattr__ of a slotted class, so either error is accepted.
        with self.assertRaises((AttributeError, TypeError)):
            s.extra = 1

    def test_seat_factory_builds_valid_seat(self):
        s = seat("eng-a", "requested", role="engineer", complexity="complex")
        self.assertEqual((s.seat, s.state, s.role, s.complexity), ("eng-a", "requested", "engineer", "complex"))
        self.assertEqual((s.generation, s.revision, s.launches), (1, 1, 0))
        self.assertIsInstance(s, dm.SeatView)

    def test_question_handoff_and_verdict_factories(self):
        question = q(7, seat="eng-a")
        self.assertIsInstance(question, dm.Question)
        self.assertEqual((question.id, question.seat), (7, "eng-a"))
        self.assertIsNotNone(question.until)
        self.assertGreater(question.until, T0)

        h = ho("feat/a@" + "a" * 40, by="a")
        self.assertIsInstance(h, dm.HandoffView)
        self.assertEqual((h.artifact, h.group, h.by), ("feat/a@" + "a" * 40, "feat/a", "a"))
        self.assertEqual(ho("report:docs/x.md@" + "b" * 40, by="b").group, "report:docs/x.md")

        verdict = v("not_met", open=2)
        self.assertIsInstance(verdict, dm.VerdictView)
        self.assertEqual((verdict.verdict, verdict.open, verdict.scope), ("not_met", 2, "artifact"))
        self.assertEqual(v("met", scope="goal").scope, "goal")
        self.assertEqual(dm.VERDICTS, ("met", "not_met", "unreachable"))

    def test_question_handoff_and_verdict_are_frozen(self):
        question = q(1)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            question.text = "changed"
        handoff = ho()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            handoff.by = "b"
        verdict = v("met")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            verdict.verdict = "not_met"
        with self.assertRaises(dataclasses.FrozenInstanceError):
            verdict.open = 3

    def test_action_is_frozen(self):
        a = dm.Action("launch", "id", "a", "fix")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            a.kind = "close"
        with self.assertRaises(dataclasses.FrozenInstanceError):
            a.ruling = True
        self.assertEqual(a.kind, "launch")

    def test_verdict_view_carries_verdict_data_keys(self):
        names = {f.name for f in dataclasses.fields(dm.VerdictView)}
        self.assertLessEqual(set(VERDICT_DATA_KEYS), names)
        self.assertEqual(v("met", candidate="feat/a@" + "c" * 40, req="R1").candidate, "feat/a@" + "c" * 40)
        self.assertEqual(v("met", req="R1").req, "R1")

    def test_seat_defaults_to_engineer_role_and_idle_state(self):
        s = seat()
        self.assertEqual((s.role, s.state), ("engineer", "idle"))

    def test_question_factory_defaults_are_a_real_question(self):
        question = q()
        self.assertTrue(question.text)
        self.assertEqual((question.job, question.generation, question.default), ("J", 1, "left"))

    def test_intent_factory_defaults_to_a_concluded_intent(self):
        self.assertEqual(intent().state, "concluded")

    def test_seat_defaults_to_haiku_and_no_watermark(self):
        s = seat()
        self.assertEqual(s.model, "haiku")
        self.assertIsNone(s.wake_watermark)
        self.assertEqual(seat(wake_watermark=42).wake_watermark, 42)

    def test_factory_defaults_generation_and_rounds(self):
        self.assertEqual(v().generation, 1)
        self.assertEqual(L().rounds_without_progress, 0)
        self.assertEqual(L().generation, 1)

    def test_snapshot_factory_defaults_are_fresh_containers(self):
        a, b = snap(), snap()
        self.assertIsNot(a.seats, b.seats)
        self.assertIsNot(a.live, b.live)
        self.assertEqual((a.job, a.generation, a.status, a.halted), ("J", 1, "active", False))
        self.assertEqual(a.now, T0)


class FixtureTest(unittest.TestCase):
    def test_ledger_stand_in_has_eng_ledger_field_names(self):
        self.assertEqual(tuple(f.name for f in dataclasses.fields(L)), LEDGER_FIELDS)

    def test_stand_ins_match_the_real_ledger_and_receipt(self):
        # Now that the ledger is integrated, pin the stand-ins to the real dataclasses, not to copies.
        from swarm import receipts
        from swarm.supervisor import ledger
        self.assertEqual(LEDGER_FIELDS, tuple(f.name for f in dataclasses.fields(ledger.Ledger)))
        self.assertEqual(INTENT_FIELDS, tuple(f.name for f in dataclasses.fields(ledger.Intent)))
        self.assertEqual(RECEIPT_FIELDS, tuple(f.name for f in dataclasses.fields(receipts.Receipt)))

    def test_intent_and_receipt_stand_ins_have_eng_ledger_field_names(self):
        self.assertEqual(tuple(f.name for f in dataclasses.fields(Intent)), INTENT_FIELDS)
        self.assertEqual(tuple(f.name for f in dataclasses.fields(Receipt)), RECEIPT_FIELDS)

    def test_ledger_incarnation_and_rev_defaults(self):
        led = L()
        self.assertEqual((led.incarnation, led.ledger_rev), ("", 0))
        self.assertEqual(L(incarnation="f" * 32, ledger_rev=4).ledger_rev, 4)

    def test_receipt_factory_defaults_are_well_formed_and_frozen(self):
        r = receipt()
        self.assertEqual((r.generation, r.rev, r.destinations), (1, 1, None))
        self.assertEqual(r.kind, "done")
        self.assertIn(r.kind, RECEIPT_KINDS)
        self.assertGreater(r.expires_at, T0.timestamp())
        self.assertEqual(len(r.incarnation), 32)
        self.assertEqual((len(r.content_sha), len(r.args_sha)), (64, 64))
        self.assertTrue(r.action_id)
        self.assertTrue(r.board_key)
        self.assertTrue(r.job)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            r.rev = 2

    def test_intent_stand_in_is_mutable_like_the_ledger_intent(self):
        i = intent()
        i.state = "concluded"
        i.charged_usd = 1.5
        self.assertEqual((i.state, i.charged_usd), ("concluded", 1.5))

    def test_verdict_progress_is_a_bool_or_none(self):
        self.assertIsNone(v("not_met").progress)
        self.assertIs(v("not_met", progress=True).progress, True)
        self.assertIs(v("not_met", progress=False).progress, False)
        self.assertEqual(typing.get_type_hints(dm.VerdictView)["progress"], bool | None)

    def test_ledger_spent_usd_seeds_a_concluded_charge(self):
        led = L(spent_usd=35.5)
        self.assertAlmostEqual(led.charged_usd(), 35.5)
        self.assertEqual([i.state for i in led.intents], ["concluded"])
        self.assertEqual(L().charged_usd(), 0.0)

    def test_charged_usd_reserves_open_intents_and_charges_concluded_ones(self):
        # reserved and charged differ, so flipping the state test changes the total
        open_reserve = L(intents=[intent("claimed", reserved=5.0, charged=None)])
        self.assertAlmostEqual(open_reserve.charged_usd(), 5.0)
        concluded = L(intents=[intent("concluded", reserved=5.0, charged=2.0)])
        self.assertAlmostEqual(concluded.charged_usd(), 2.0)
        # a concluded intent with no charge (no usage reported) charges nothing, not its reservation
        no_usage = L(intents=[intent("concluded", reserved=5.0, charged=None)])
        self.assertAlmostEqual(no_usage.charged_usd(), 0.0)
        mixed = L(intents=[intent("concluded", reserved=5.0, charged=2.0),
                           intent("claimed", reserved=3.0, charged=None, seat="b")])
        self.assertAlmostEqual(mixed.charged_usd(), 5.0)

    def test_ledger_rejects_unknown_fields(self):
        with self.assertRaises(TypeError):
            L(spent=1)

    def test_iso_and_minutes(self):
        self.assertEqual(dt.datetime.fromisoformat(iso(T0)), T0)
        self.assertEqual(iso(T0 - minutes(16)), (T0 - dt.timedelta(minutes=16)).isoformat())
        self.assertEqual(minutes(121), dt.timedelta(minutes=121))
        self.assertEqual(L(ruling_due_at=iso(T0)).ruling_due_at, T0.isoformat())


if __name__ == "__main__":
    unittest.main()
