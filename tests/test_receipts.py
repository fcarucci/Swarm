"""Control-record receipts (spec 4.5): the host's proof that a drive session's CLI command created a
record, with the arguments bound by hash. Each case runs under a temporary HOME inside the suite
sandbox, so nothing touches the real host directory."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import SANDBOX, home_env  # noqa: F401

from swarm import paths, receipts  # noqa: E402

BOARD = "bk"
ARGV = ["done", "--artifact", "b@" + "a" * 40]


class ReceiptCase(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(dir=SANDBOX, prefix="receipt-home-"))
        self.env = mock.patch.dict(os.environ, home_env(self.home))
        self.env.start()
        self.addCleanup(self.env.stop)

    def receipt(self, **kw) -> receipts.Receipt:
        args = dict(job="J", generation=1, action_id="1|seat|a", attempt=1, seat="a", kind="done",
                    record_id="42", args_sha=receipts.args_sha(ARGV))
        args.update(kw)
        return receipts.Receipt(**args)

    def enrolled(self) -> Path:
        return paths.host_dir() / "enrolled"


class ReceiptTests(ReceiptCase):
    def test_receipt_binds_args(self):
        r = receipts.Receipt("J", 1, "1|seat|a", 1, "a", "done", "42", receipts.args_sha(ARGV))
        receipts.write(BOARD, r)
        self.assertEqual(receipts.find(BOARD, "J", "done", "42"), r)
        self.assertIsNone(receipts.find(BOARD, "J", "done", "43"))

    def test_args_sha_is_sha256_of_nul_joined_argv(self):
        expected = hashlib.sha256("\0".join(ARGV).encode("utf-8")).hexdigest()
        self.assertEqual(receipts.args_sha(ARGV), expected)
        self.assertNotEqual(receipts.args_sha(ARGV), receipts.args_sha(ARGV[:-1]))

    def test_caller_can_detect_changed_arguments(self):
        receipts.write(BOARD, self.receipt())
        found = receipts.find(BOARD, "J", "done", "42")
        self.assertEqual(found.args_sha, receipts.args_sha(ARGV))
        self.assertNotEqual(found.args_sha, receipts.args_sha(ARGV + ["--extra"]))

    def test_receipts_are_scoped_to_the_board(self):
        receipts.write(BOARD, self.receipt())
        self.assertIsNone(receipts.find("other-board", "J", "done", "42"))

    def test_write_replaces_the_receipt_for_the_same_key(self):
        receipts.write(BOARD, self.receipt(seat="a"))
        receipts.write(BOARD, self.receipt(seat="b"))
        self.assertEqual(receipts.find(BOARD, "J", "done", "42").seat, "b")

    def test_write_stores_a_version_1_file_in_the_enrolled_directory(self):
        path = receipts.write(BOARD, self.receipt())
        self.assertEqual(path.parent, self.enrolled())
        self.assertTrue(path.name.startswith("receipt-") and path.name.endswith(".json"))
        data = json.loads(path.read_bytes())
        self.assertEqual(data["v"], 1)
        self.assertEqual(data["record_id"], "42")

    def test_invalid_kind_is_refused_and_nothing_is_written(self):
        with self.assertRaises(ValueError):
            receipts.write(BOARD, self.receipt(kind="bogus"))
        self.assertIsNone(receipts.find(BOARD, "J", "bogus", "42"))

    def test_args_sha_must_be_64_hex_digits(self):
        with self.assertRaises(ValueError):
            receipts.write(BOARD, self.receipt(args_sha="not-a-digest"))

    def test_negative_generation_is_refused(self):
        with self.assertRaises(ValueError):
            receipts.write(BOARD, self.receipt(generation=-1))

    def test_bool_is_not_an_integer_field(self):
        with self.assertRaises(ValueError):
            receipts.write(BOARD, self.receipt(attempt=True))

    def test_control_characters_in_record_id_are_refused(self):
        with self.assertRaises(ValueError):
            receipts.write(BOARD, self.receipt(record_id="42\n43"))

    def test_planted_garbage_receipt_is_not_found(self):
        path = receipts.write(BOARD, self.receipt())
        path.write_text("{not json")
        self.assertIsNone(receipts.find(BOARD, "J", "done", "42"))

    def test_receipt_planted_under_another_key_is_not_found(self):
        path = receipts.write(BOARD, self.receipt(record_id="43"))
        planted = self.enrolled() / receipts.receipt_name(BOARD, "J", "done", "42")
        planted.write_bytes(path.read_bytes())
        self.assertIsNone(receipts.find(BOARD, "J", "done", "42"))

    def test_symlinked_receipt_is_not_followed(self):
        target = self.home / "target.json"
        target.write_bytes(receipts.write(BOARD, self.receipt()).read_bytes())
        planted = self.enrolled() / receipts.receipt_name(BOARD, "J", "done", "99")
        os.symlink(target, planted)
        self.assertIsNone(receipts.find(BOARD, "J", "done", "99"))

    def test_land_receipt_carries_destinations(self):
        dest = {"https://example.test/r.git": {"state": "pushed", "sha": "a" * 40}}
        r = self.receipt(kind="land", record_id="7", destinations=dest)
        receipts.write(BOARD, r)
        found = receipts.find(BOARD, "J", "land", "7")
        self.assertEqual(found.destinations, dest)

    def test_destination_state_must_be_known(self):
        dest = {"https://example.test/r.git": {"state": "merged", "sha": "a" * 40}}
        with self.assertRaises(ValueError):
            receipts.write(BOARD, self.receipt(kind="land", record_id="7", destinations=dest))

    def test_destinations_only_on_land_receipts(self):
        dest = {"https://example.test/r.git": {"state": "pushed", "sha": "a" * 40}}
        with self.assertRaises(ValueError):
            receipts.write(BOARD, self.receipt(kind="done", destinations=dest))


if __name__ == "__main__":
    unittest.main()
