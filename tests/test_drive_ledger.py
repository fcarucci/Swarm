"""The host-private drive ledger (spec 4.0): roundtrip, charges, strict loading, planted links,
fsync discipline. Each case runs under a temporary HOME inside the suite sandbox."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import SANDBOX, home_env  # noqa: F401

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

    def test_concluded_intent_charges_actual_cost_open_ones_charge_reservation(self):
        led = ledger.create("bk", "J", budgets={}, activated_at=ACTIVATED)
        led.intents.append(ledger.Intent("L1", "a", 1, "a", 5.0, 60.0, [], "concluded",
                                         outcome="done", success=True, charged_usd=2.25))
        led.intents.append(ledger.Intent("L2", "b", 1, "b", 4.0, 30.0, [], "claimed"))
        led.save()
        self.assertEqual(ledger.load("bk", "J").charged_usd(), 2.25 + 4.0)

    def test_on_disk_file_is_version_1_json_named_by_key(self):
        ledger.create("bk", "J", budgets={}, activated_at=ACTIVATED)
        data = json.loads(self.path_of("bk", "J").read_bytes())
        self.assertEqual(data["v"], 1)
        self.assertEqual(data["job"], "J")
        self.assertEqual(data["board_key"], "bk")

    def test_missing_ledger_loads_as_none(self):
        self.assertIsNone(ledger.load("bk", "nope"))

    def test_terminal_ledgers_are_not_unfinished(self):
        led = ledger.create("bk", "J", budgets={}, activated_at=ACTIVATED)
        led.terminal = {"verdict": "met"}
        led.save()
        self.assertEqual(ledger.unfinished("bk"), [])

    def test_unfinished_is_per_board(self):
        ledger.create("bk", "J", budgets={}, activated_at=ACTIVATED)
        ledger.create("other", "K", budgets={}, activated_at=ACTIVATED)
        self.assertEqual([x.job for x in ledger.unfinished("bk")], ["J"])
        self.assertEqual([x.job for x in ledger.unfinished("other")], ["K"])

    def test_planted_symlink_refused(self):
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
        obj = json.loads(self.path_of("bk", "J").read_bytes())
        obj["surprise"] = 1
        self.path_of("bk", "J").write_text(json.dumps(obj))
        with self.assertRaises(ledger.LedgerBadError):
            ledger.load("bk", "J")
        del obj["surprise"]
        del obj["watermarks"]
        self.path_of("bk", "J").write_text(json.dumps(obj))
        with self.assertRaises(ledger.LedgerBadError):
            ledger.load("bk", "J")

    def test_malformed_intent_is_refused(self):
        led = ledger.create("bk", "J", budgets={}, activated_at=ACTIVATED)
        led.intents.append(ledger.Intent("L1", "a", 1, None, 5.0, 60.0, [], "claimed"))
        led.save()
        obj = json.loads(self.path_of("bk", "J").read_bytes())
        obj["intents"][0]["state"] = "half-done"
        self.path_of("bk", "J").write_text(json.dumps(obj))
        with self.assertRaises(ledger.LedgerBadError):
            ledger.load("bk", "J")

    def test_ledger_for_another_key_is_refused(self):
        ledger.create("bk", "J", budgets={}, activated_at=ACTIVATED)
        # A copy of the file planted under the name of another job.
        self.path_of("bk", "K").write_bytes(self.path_of("bk", "J").read_bytes())
        with self.assertRaises(ledger.LedgerBadError):
            ledger.load("bk", "K")

    def test_planted_symlink_in_drive_dir_is_reported_not_skipped(self):
        ledger.create("bk", "J", budgets={}, activated_at=ACTIVATED)
        target = self.home / "elsewhere.json"
        target.write_bytes(self.path_of("bk", "J").read_bytes())
        planted = self.drive_dir() / _file_name("bk", "K")
        os.symlink(target, planted)
        with self.assertRaises(ledger.LedgerBadError):
            ledger.load("bk", "K")
        self.assertIn(_file_name("bk", "K"), [b.path for b in ledger.unfinished("bk")
                                             if isinstance(b, ledger.LedgerBad)])

    def test_unfinished_reports_a_ledger_filed_under_another_key(self):
        ledger.create("bk", "J", budgets={}, activated_at=ACTIVATED)
        # J's valid content, planted under K's file name: never listed as K's (or as J's) ledger.
        self.path_of("bk", "K").write_bytes(self.path_of("bk", "J").read_bytes())
        found = ledger.unfinished("bk")
        self.assertEqual(sorted(type(x).__name__ for x in found), ["Ledger", "LedgerBad"])
        bad = [x for x in found if isinstance(x, ledger.LedgerBad)]
        self.assertEqual(bad[0].path, _file_name("bk", "K"))
        self.assertEqual([x.job for x in found if isinstance(x, ledger.Ledger)], ["J"])

    def test_save_fsyncs_file_and_directory(self):
        led = ledger.create("bk", "J", budgets={}, activated_at=ACTIVATED)
        real_fsync = os.fsync
        with mock.patch("os.fsync", side_effect=real_fsync) as fsync:
            led.save()
        self.assertEqual(fsync.call_count, 2)


if __name__ == "__main__":
    unittest.main()
