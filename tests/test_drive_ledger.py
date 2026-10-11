"""The host-private drive ledger (spec 4.0): roundtrip, charges, strict loading, exclusive creation,
optimistic concurrency, planted links, durability and value validation. Each case runs under a
temporary HOME inside the suite sandbox."""
from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from support import SANDBOX, home_env, posix_only  # noqa: F401

from swarm import compat  # noqa: E402
from swarm.supervisor import ledger, settings  # noqa: E402

ACTIVATED = "2026-10-10T00:00:00+00:00"


def _file_name(board_key: str, job: str) -> str:
    return hashlib.sha256(f"{board_key}\0{job}".encode("utf-8")).hexdigest() + ".json"


class LedgerCase(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(dir=SANDBOX, prefix="ledger-home-"))
        self.env = mock.patch.dict(os.environ, home_env(self.home))
        self.env.start()
        self.addCleanup(self.env.stop)

    def drive_dir(self) -> Path:
        return settings.private_dir() / "drive"

    def path_of(self, board_key: str, job: str) -> Path:
        return self.drive_dir() / _file_name(board_key, job)

    def raw(self, board_key: str, job: str) -> dict:
        return json.loads(self.path_of(board_key, job).read_bytes())

    def put(self, board_key: str, job: str, obj: dict) -> None:
        self.path_of(board_key, job).write_text(json.dumps(obj))

    def good(self, **kw) -> ledger.Ledger:
        """A fresh ledger for ("bk", "J"), replacing any file a previous step of the test left."""
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.path_of("bk", "J"))
        led = ledger.create("bk", "J", budgets={"max_cost_usd": 40}, activated_at=ACTIVATED)
        for k, v in kw.items():
            setattr(led, k, v)
        led.save()
        return led


class LedgerTests(LedgerCase):
    def test_roundtrip_and_charges(self):
        led = ledger.create("bk", "J", budgets={"max_cost_usd": 40}, activated_at=ACTIVATED)
        led.intents.append(ledger.Intent("L1", "1|seat|a", 1, "a", 5.0, 60.0, [], "claimed"))
        led.save()
        again = ledger.load("bk", "J")
        self.assertIsNotNone(again)
        self.assertEqual(again.charged_usd(), 5.0)
        self.assertEqual(again.intents, [ledger.Intent("L1", "1|seat|a", 1, "a", 5.0, 60.0, [], "claimed")])
        self.assertEqual(again.budgets, {"max_cost_usd": 40})
        self.assertEqual([x.job for x in ledger.unfinished("bk")], ["J"])

    def test_last_progress_at_roundtrips_and_is_validated(self):
        led = self.good(last_progress_at="2026-10-10T01:00:00+00:00")
        self.assertEqual(ledger.load("bk", "J").last_progress_at, "2026-10-10T01:00:00+00:00")
        self.assertIsNone(ledger.create("bk", "K", budgets={}, activated_at=ACTIVATED).last_progress_at)
        for bad in (5, "x\x00y", "t" * 65, ""):
            obj = self.raw("bk", "J")
            obj["last_progress_at"] = bad
            self.put("bk", "J", obj)
            with self.assertRaises(ledger.LedgerBadError, msg=repr(bad)):
                ledger.load("bk", "J")

    def test_concluded_intent_charges_actual_cost_open_ones_charge_reservation(self):
        led = ledger.create("bk", "J", budgets={}, activated_at=ACTIVATED)
        led.intents.append(ledger.Intent("L1", "a", 1, "a", 5.0, 60.0, [], "concluded",
                                         outcome="done", success=True, charged_usd=2.25))
        led.intents.append(ledger.Intent("L2", "b", 1, "b", 4.0, 30.0, [], "claimed"))
        led.save()
        self.assertEqual(ledger.load("bk", "J").charged_usd(), 2.25 + 4.0)

    def test_on_disk_file_is_version_1_json_named_by_key(self):
        ledger.create("bk", "J", budgets={}, activated_at=ACTIVATED)
        data = self.raw("bk", "J")
        self.assertEqual(data["v"], 1)
        self.assertEqual(data["job"], "J")
        self.assertEqual(data["board_key"], "bk")

    def test_missing_ledger_loads_as_none(self):
        self.assertIsNone(ledger.load("bk", "nope"))

    def test_terminal_ledgers_are_not_unfinished(self):
        led = ledger.create("bk", "J", budgets={}, activated_at=ACTIVATED)
        led.terminal = {"verdict": "met", "at": ACTIVATED}
        led.save()
        self.assertEqual(ledger.unfinished("bk"), [])

    def test_unfinished_is_per_board(self):
        ledger.create("bk", "J", budgets={}, activated_at=ACTIVATED)
        ledger.create("other", "K", budgets={}, activated_at=ACTIVATED)
        self.assertEqual([x.job for x in ledger.unfinished("bk")], ["J"])
        self.assertEqual([x.job for x in ledger.unfinished("other")], ["K"])

    @posix_only("Windows has no O_NOFOLLOW: a junction at drive/ is followed there (see the ledger docstring)")
    def test_planted_symlink_drive_dir_refused(self):
        self.drive_dir().parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.symlink("/etc", self.drive_dir())
        with self.assertRaises(settings.PrivateDirError):
            ledger.create("bk", "J", budgets={}, activated_at="x")

    def test_garbage_file_is_refused_by_load_and_reported_by_unfinished(self):
        ledger.create("bk", "J", budgets={}, activated_at=ACTIVATED)
        self.path_of("bk", "J").write_text("this is not json")
        with self.assertRaises(ledger.LedgerBadError):
            ledger.load("bk", "J")
        bad = ledger.unfinished("bk")
        self.assertEqual(len(bad), 1)
        self.assertIsInstance(bad[0], ledger.LedgerBad)
        self.assertEqual(bad[0].path, _file_name("bk", "J"))

    def test_unknown_or_missing_field_is_refused(self):
        ledger.create("bk", "J", budgets={}, activated_at=ACTIVATED)
        obj = self.raw("bk", "J")
        obj["surprise"] = 1
        self.put("bk", "J", obj)
        with self.assertRaises(ledger.LedgerBadError):
            ledger.load("bk", "J")
        del obj["surprise"]
        del obj["watermarks"]
        self.put("bk", "J", obj)
        with self.assertRaises(ledger.LedgerBadError):
            ledger.load("bk", "J")

    def test_ledger_version_must_be_1(self):
        ledger.create("bk", "J", budgets={}, activated_at=ACTIVATED)
        obj = self.raw("bk", "J")
        obj["v"] = 2
        self.put("bk", "J", obj)
        with self.assertRaises(ledger.LedgerBadError):
            ledger.load("bk", "J")

    def test_malformed_intent_is_refused(self):
        led = ledger.create("bk", "J", budgets={}, activated_at=ACTIVATED)
        led.intents.append(ledger.Intent("L1", "a", 1, None, 5.0, 60.0, [], "claimed"))
        led.save()
        obj = self.raw("bk", "J")
        obj["intents"][0]["state"] = "half-done"
        self.put("bk", "J", obj)
        with self.assertRaises(ledger.LedgerBadError):
            ledger.load("bk", "J")

    def test_ledger_for_another_key_is_refused(self):
        ledger.create("bk", "J", budgets={}, activated_at=ACTIVATED)
        # A copy of the file planted under the name of another job.
        self.path_of("bk", "K").write_bytes(self.path_of("bk", "J").read_bytes())
        with self.assertRaises(ledger.LedgerBadError):
            ledger.load("bk", "K")

    @posix_only("Windows has no O_NOFOLLOW: a link is followed there (see the ledger docstring)")
    def test_symlink_to_a_valid_same_key_ledger_is_refused_for_being_a_link(self):
        # The strongest case: the link's target is a well-formed ledger for exactly this key, so
        # only the refusal of the link itself can make load() fail.
        ledger.create("bk", "K", budgets={}, activated_at=ACTIVATED)
        target = self.home / "elsewhere.json"
        target.write_bytes(self.path_of("bk", "K").read_bytes())
        os.unlink(self.path_of("bk", "K"))
        os.symlink(target, self.path_of("bk", "K"))
        with self.assertRaisesRegex(ledger.LedgerBadError, "not a private regular file"):
            ledger.load("bk", "K")
        found = ledger.unfinished("bk")
        self.assertEqual([b.path for b in found if isinstance(b, ledger.LedgerBad)], [_file_name("bk", "K")])
        self.assertEqual([x for x in found if isinstance(x, ledger.Ledger)], [])

    def test_save_fsyncs_file_and_directory(self):
        led = ledger.create("bk", "J", budgets={}, activated_at=ACTIVATED)
        real_fsync = os.fsync
        with mock.patch("os.fsync", side_effect=real_fsync) as fsync:
            led.save()
        # file and directory; Windows cannot flush a directory (compat.fsync_dir is a no-op there)
        self.assertEqual(fsync.call_count, 1 if sys.platform == "win32" else 2)

    def test_save_raises_when_the_directory_fsync_fails(self):
        led = ledger.create("bk", "J", budgets={}, activated_at=ACTIVATED)
        with mock.patch.object(compat, "fsync_dir", side_effect=OSError(5, "EIO")):
            with self.assertRaises(OSError):
                led.save()

    def test_unfinished_reports_a_ledger_filed_under_another_key(self):
        ledger.create("bk", "J", budgets={}, activated_at=ACTIVATED)
        # J's valid content, planted under K's file name: never listed as K's (or as J's) ledger.
        self.path_of("bk", "K").write_bytes(self.path_of("bk", "J").read_bytes())
        found = ledger.unfinished("bk")
        self.assertEqual(sorted(type(x).__name__ for x in found), ["Ledger", "LedgerBad"])
        bad = [x for x in found if isinstance(x, ledger.LedgerBad)]
        self.assertEqual(bad[0].path, _file_name("bk", "K"))
        self.assertEqual([x.job for x in found if isinstance(x, ledger.Ledger)], ["J"])


class ExclusiveCreateTests(LedgerCase):
    def test_create_over_an_existing_ledger_raises_and_keeps_its_charges(self):
        led = ledger.create("bk", "J", budgets={}, activated_at=ACTIVATED)
        led.intents.append(ledger.Intent("L1", "a", 1, "a", 5.0, 60.0, [], "claimed"))
        led.save()
        with self.assertRaises(ledger.LedgerExistsError):
            ledger.create("bk", "J", budgets={}, activated_at=ACTIVATED)
        self.assertEqual(ledger.load("bk", "J").charged_usd(), 5.0)

    def test_create_over_a_bad_file_raises_and_leaves_the_file_in_place(self):
        self.drive_dir().parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.drive_dir().mkdir(mode=0o700)
        self.path_of("bk", "J").write_text("garbage that must survive")
        with self.assertRaises(ledger.LedgerExistsError):
            ledger.create("bk", "J", budgets={}, activated_at=ACTIVATED)
        self.assertEqual(self.path_of("bk", "J").read_text(), "garbage that must survive")

    def test_reactivation_merges_budgets_keeping_keys_not_given(self):
        # Pinned: activation budgets are a merge. Keys given overwrite, keys not given keep their
        # value (a per-job override changes what it names, not the job's other limits).
        ledger.create("bk", "J", budgets={"max_cost_usd": 40, "max_sessions": 60}, activated_at=ACTIVATED)
        ledger.activate("bk", "J", budgets={"max_cost_usd": 20}, activated_at=ACTIVATED)
        self.assertEqual(ledger.load("bk", "J").budgets, {"max_cost_usd": 20, "max_sessions": 60})

    def test_activate_creates_when_absent(self):
        led = ledger.activate("bk", "J", budgets={"max_cost_usd": 40}, activated_at=ACTIVATED)
        self.assertEqual(led.generation_requested, 1)
        self.assertEqual(ledger.load("bk", "J").budgets, {"max_cost_usd": 40})

    def test_reactivation_loads_bumps_the_generation_and_keeps_intents(self):
        led = ledger.create("bk", "J", budgets={"max_cost_usd": 40}, activated_at=ACTIVATED)
        led.intents.append(ledger.Intent("L1", "a", 1, "a", 5.0, 60.0, [], "claimed"))
        led.save()
        again = ledger.activate("bk", "J", budgets={"max_cost_usd": 20}, activated_at="2026-10-11T00:00:00+00:00")
        self.assertEqual(again.generation_requested, 2)
        stored = ledger.load("bk", "J")
        self.assertEqual(stored.generation_requested, 2)
        self.assertEqual(stored.charged_usd(), 5.0)
        self.assertEqual(stored.budgets, {"max_cost_usd": 20})
        self.assertEqual(stored.activated_at, "2026-10-11T00:00:00+00:00")   # a rollover starts a new window

    def test_activate_refuses_a_finished_job_and_changes_nothing(self):
        led = ledger.create("bk", "J", budgets={}, activated_at=ACTIVATED)
        led.terminal = {"verdict": "met", "at": ACTIVATED}
        led.save()
        before = self.path_of("bk", "J").read_bytes()
        with self.assertRaises(ledger.LedgerFinishedError):
            ledger.activate("bk", "J", budgets={}, activated_at="2026-10-11T00:00:00+00:00")
        self.assertEqual(self.path_of("bk", "J").read_bytes(), before)

    def test_reactivation_over_a_bad_file_is_refused(self):
        self.drive_dir().parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.drive_dir().mkdir(mode=0o700)
        self.path_of("bk", "J").write_text("garbage")
        with self.assertRaises(ledger.LedgerBadError):
            ledger.activate("bk", "J", budgets={}, activated_at=ACTIVATED)
        self.assertEqual(self.path_of("bk", "J").read_text(), "garbage")

    def test_each_creation_gets_a_fresh_incarnation(self):
        first = ledger.create("bk", "J", budgets={}, activated_at=ACTIVATED).incarnation
        os.unlink(self.path_of("bk", "J"))   # a deleted and re-created job: a new incarnation
        second = ledger.create("bk", "J", budgets={}, activated_at=ACTIVATED).incarnation
        self.assertNotEqual(first, second)
        for inc in (first, second):
            self.assertRegex(inc, r"[0-9a-f]{32}")

    def test_reactivation_keeps_the_incarnation(self):
        inc = ledger.create("bk", "J", budgets={}, activated_at=ACTIVATED).incarnation
        self.assertEqual(ledger.activate("bk", "J", budgets={}, activated_at=ACTIVATED).incarnation, inc)

    def test_incarnation_must_be_32_hex_digits(self):
        ledger.create("bk", "J", budgets={}, activated_at=ACTIVATED)
        obj = self.raw("bk", "J")
        obj["incarnation"] = "not-hex"
        self.put("bk", "J", obj)
        with self.assertRaises(ledger.LedgerBadError):
            ledger.load("bk", "J")


class ConcurrencyTests(LedgerCase):
    def test_a_stale_copy_cannot_overwrite_a_newer_save(self):
        ledger.create("bk", "J", budgets={}, activated_at=ACTIVATED)
        a = ledger.load("bk", "J")
        b = ledger.load("bk", "J")
        b.intents.append(ledger.Intent("B", "b", 1, "b", 1.0, 1.0, [], "claimed"))
        b.save()
        a.intents.append(ledger.Intent("A", "a", 1, "a", 9.0, 1.0, [], "claimed"))
        with self.assertRaises(ledger.LedgerConflict):
            a.save()
        stored = ledger.load("bk", "J")
        self.assertEqual([i.launch_id for i in stored.intents], ["B"])
        self.assertEqual(stored.charged_usd(), 1.0)

    def test_a_stale_copy_of_a_deleted_job_cannot_overwrite_its_successor(self):
        ledger.create("bk", "J", budgets={}, activated_at=ACTIVATED)
        old = ledger.load("bk", "J")                       # rev 1, incarnation A
        os.unlink(self.path_of("bk", "J"))
        successor = ledger.create("bk", "J", budgets={}, activated_at=ACTIVATED)   # rev 1, incarnation B
        self.assertEqual(successor.ledger_rev, old.ledger_rev)
        self.assertNotEqual(successor.incarnation, old.incarnation)
        old.intents.append(ledger.Intent("OLD", "a", 1, None, 9.0, 1.0, [], "claimed"))
        with self.assertRaises(ledger.LedgerConflict):
            old.save()
        self.assertEqual(ledger.load("bk", "J").incarnation, successor.incarnation)
        self.assertEqual(ledger.load("bk", "J").intents, [])

    def test_load_modify_save_by_many_threads_loses_no_intent(self):
        ledger.create("bk", "J", budgets={}, activated_at=ACTIVATED)
        errors: list = []

        def writer(n: int) -> None:
            try:
                for attempt in range(200):
                    led = ledger.load("bk", "J")
                    led.intents.append(ledger.Intent(f"T{n}-{attempt}", "a", 1, None, 0.5, 1.0, [], "claimed"))
                    try:
                        led.save()
                        return
                    except ledger.LedgerConflict:
                        continue
                errors.append(f"thread {n}: never saved")
            except Exception as exc:   # surfaced below
                errors.append(repr(exc))

        threads = [threading.Thread(target=writer, args=(n,)) for n in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(ledger.load("bk", "J").intents), 6)

    def test_a_stale_save_is_refused_until_reloaded(self):
        ledger.create("bk", "J", budgets={}, activated_at=ACTIVATED)
        a = ledger.load("bk", "J")
        ledger.load("bk", "J").save()
        with self.assertRaises(ledger.LedgerConflict):
            a.save()
        ledger.load("bk", "J").save()   # a reloaded copy saves fine


class ValidationTests(LedgerCase):
    def assertRefused(self, mutate, why: str):
        self.good()
        obj = self.raw("bk", "J")
        mutate(obj)
        self.put("bk", "J", obj)
        with self.subTest(why), self.assertRaises(ledger.LedgerBadError):
            ledger.load("bk", "J")

    def test_negative_amounts_are_refused(self):
        def intent(field, value):
            def m(obj):
                obj["intents"] = [{"launch_id": "L", "action_id": "a", "attempt": 1, "seat": None,
                                   "reserved_usd": 1.0, "reserved_minutes": 1.0, "locks": [],
                                   "state": "claimed", "outcome": None, "success": None,
                                   "charged_usd": None}]
                obj["intents"][0][field] = value
            return m
        for field, value in [("reserved_usd", -5.0), ("reserved_minutes", -1.0), ("attempt", -1),
                             ("charged_usd", -500.0)]:
            self.assertRefused(intent(field, value), f"intent {field}={value}")
        self.assertRefused(lambda o: o.update(halted_total_seconds=-1.0), "halted_total_seconds")
        self.assertRefused(lambda o: o.update(errors=-1), "errors")
        self.assertRefused(lambda o: o.update(generation=0), "generation 0")
        self.assertRefused(lambda o: o.update(watermarks={"seat": -3}), "watermark")
        self.assertRefused(lambda o: o.update(budgets={"max_cost_usd": -40}), "budget")

    def test_non_finite_amounts_are_refused(self):
        self.assertRefused(lambda o: o.update(halted_total_seconds=float("inf")), "inf")
        self.assertRefused(lambda o: o.update(budgets={"max_cost_usd": math.nan}), "nan budget")

    def test_halted_total_seconds_must_be_a_number_not_a_string_or_bool(self):
        self.assertRefused(lambda o: o.update(halted_total_seconds="x"), "string")
        self.assertRefused(lambda o: o.update(halted_total_seconds=True), "bool")

    def test_concluded_intent_must_carry_a_charge(self):
        def m(obj):
            obj["intents"] = [{"launch_id": "L", "action_id": "a", "attempt": 1, "seat": None,
                               "reserved_usd": 1.0, "reserved_minutes": 1.0, "locks": [],
                               "state": "concluded", "outcome": "done", "success": True,
                               "charged_usd": None}]
        self.assertRefused(m, "concluded without a charge")

    def test_terminal_intent_has_a_fixed_shape(self):
        good = {"verdict": "met", "scope": "goal", "generation": 1, "candidate": "b@" + "a" * 40,
                "req": None, "by": "judge", "at": ACTIVATED}
        self.good(terminal_intent=good)
        self.assertEqual(ledger.load("bk", "J").terminal_intent, good)
        for bad in [dict(good, extra=1), {k: v for k, v in good.items() if k != "by"},
                    dict(good, verdict="maybe"), dict(good, generation=-1), dict(good, by=5)]:
            self.assertRefused(lambda o, b=bad: o.update(terminal_intent=b), f"terminal_intent {sorted(bad)}")

    def test_terminal_and_halted_have_checked_shapes(self):
        self.assertRefused(lambda o: o.update(terminal={"verdict": "fine"}), "terminal verdict")
        self.assertRefused(lambda o: o.update(terminal={"verdict": "met"}), "terminal without at")
        self.assertRefused(lambda o: o.update(halted={"at": ACTIVATED}), "halted without by")
        self.good(halted={"by": "Francesco", "at": ACTIVATED, "reason": None})
        self.assertEqual(ledger.load("bk", "J").halted["by"], "Francesco")

    def test_integrated_and_budget_values_are_typed(self):
        self.assertRefused(lambda o: o.update(integrated={"g": "not-a-dict"}), "integrated value")
        self.assertRefused(lambda o: o.update(budgets={"max_cost_usd": "40"}), "budget string")
        self.assertRefused(lambda o: o.update(owners={"g": 3}), "owner value")

    def test_unsafe_keys_are_refused_by_create(self):
        for bk, job in [("a\0b", "J"), ("bk", "J\n"), ("bk", "J‮"), ("", "J"), ("bk", ""),
                        ("bk", "x" * 257), ("k" * 1025, "J")]:
            with self.subTest(board_key=bk[:12], job=job[:12]), self.assertRaises(ValueError):
                ledger.create(bk, job, budgets={}, activated_at=ACTIVATED)
        self.assertFalse(self.drive_dir().exists() and any(self.drive_dir().iterdir()))

    def test_save_refuses_an_invalid_ledger_and_writes_nothing(self):
        # the guard in save(): a value load would refuse must not reach the disk
        led = self.good()
        before = self.path_of("bk", "J").read_bytes()
        led.intents.append(ledger.Intent("L", "a", 1, None, 5.0, 1.0, [], "concluded",
                                         outcome="done", success=True, charged_usd=-500.0))
        with self.assertRaises(ValueError):
            led.save()
        self.assertEqual(self.path_of("bk", "J").read_bytes(), before)
        self.assertEqual(ledger.load("bk", "J").charged_usd(), 0.0)

    def test_save_refuses_a_nul_in_a_key_field_and_writes_nothing(self):
        led = self.good()
        before = self.path_of("bk", "J").read_bytes()
        led.owners = {"group\0x": "seat"}
        with self.assertRaises(ValueError):
            led.save()
        self.assertEqual(self.path_of("bk", "J").read_bytes(), before)

    def test_a_planted_nul_key_in_the_body_is_refused(self):
        self.good()
        obj = self.raw("bk", "J")
        obj["job"] = "J\0x"
        self.put("bk", "J", obj)
        with self.assertRaises(ledger.LedgerBadError):
            ledger.load("bk", "J")


if __name__ == "__main__":
    unittest.main()
