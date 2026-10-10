"""The frozen drive-driver model (spec 3.2-3.4, plan Task 7a): exact field lists, frozen and slotted
dataclasses, and the factories in drive_fixtures that Tasks 7-8c build on."""
from __future__ import annotations

import dataclasses
import datetime as dt
import unittest

import support  # noqa: F401  (puts lib/ on sys.path)
from drive_fixtures import L, T0, ho, iso, minutes, q, seat, snap, v
from swarm.supervisor import driver_model as dm

# Field names of eng-ledger's Ledger dataclass (lib/swarm/supervisor/ledger.py @ 60f507d).
LEDGER_FIELDS = (
    "board_key", "job", "generation", "generation_requested", "activated_at", "budgets", "intents",
    "owners", "integrated", "ruling_due_at", "ruling_attempts", "rounds_without_progress", "errors",
    "closing", "halted", "terminal", "mirror_fetch_pending", "last_pass", "watermarks",
    "terminal_intent", "halted_total_seconds", "requester_session", "pinned_requirements",
)

SNAPSHOT_FIELDS = (
    "job", "generation", "goal", "status", "halted", "ledger", "seats", "live", "handoffs", "reviews",
    "verdicts", "goal_verdict", "goal_ready", "questions", "wakes", "integrated", "now",
)

ACTION_FIELDS = (
    "kind", "action_id", "seat", "prompt_kind", "artifact", "ruling", "reason", "not_before", "responsible",
)

# spec 3.4 `seats` table, schema 25
SEAT_FIELDS = (
    "id", "job", "seat", "role", "title", "harness", "model", "workdir", "brief_path", "task_ref",
    "complexity", "requested_by", "requested_at", "state", "agent_key", "session_id", "generation",
    "revision", "launches", "last_outcome", "wake_reason", "updated_at",
)

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
            (dm.HandoffView, ho), (dm.VerdictView, v), (L, L),
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

    def test_snapshot_factory_defaults_are_fresh_containers(self):
        a, b = snap(), snap()
        self.assertIsNot(a.seats, b.seats)
        self.assertIsNot(a.live, b.live)
        self.assertEqual((a.job, a.generation, a.status, a.halted), ("J", 1, "active", False))
        self.assertEqual(a.now, T0)


class FixtureTest(unittest.TestCase):
    def test_ledger_stand_in_has_eng_ledger_field_names(self):
        self.assertEqual(tuple(f.name for f in dataclasses.fields(L)), LEDGER_FIELDS)

    def test_ledger_spent_usd_seeds_a_concluded_charge(self):
        led = L(spent_usd=35.5)
        self.assertAlmostEqual(led.charged_usd(), 35.5)
        self.assertEqual([i.state for i in led.intents], ["concluded"])
        self.assertEqual(L().charged_usd(), 0.0)

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
