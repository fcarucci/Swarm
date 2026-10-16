"""Control-record receipts (spec 4.5): the host's proof that a drive session's CLI command created a
record. A receipt binds the job, board, incarnation, generation, the record's id and revision, a
digest of the record's decision fields (content_sha) and the argv hash (audit only), and it expires.
Each case runs under a temporary HOME inside the suite sandbox."""
from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import SANDBOX, home_env, posix_only  # noqa: F401

from swarm import compat, paths, receipts  # noqa: E402

BOARD = "bk"
INC = "0123456789abcdef0123456789abcdef"
NOW = 1_800_000_000.0
ARGV = ["done", "--artifact", "b@" + "a" * 40]
DONE = {"artifact": "b@" + "a" * 40, "sha": "a" * 40, "group": "g1"}


class ReceiptCase(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(dir=SANDBOX, prefix="receipt-home-"))
        self.env = mock.patch.dict(os.environ, home_env(self.home))
        self.env.start()
        self.addCleanup(self.env.stop)

    def receipt(self, **kw) -> receipts.Receipt:
        args = dict(board_key=BOARD, job="J", generation=1, incarnation=INC, action_id="1|launch|a",
                    attempt=1, launch="a", kind="done", record_id="42", rev=1,
                    content_sha=receipts.content_sha("done", DONE), args_sha=receipts.args_sha(ARGV),
                    expires_at=NOW + 3600)
        args.update(kw)
        return receipts.Receipt(**args)

    def row(self, **kw) -> dict:
        """The board row as the hook reads it back: kind, id, rev and the decision fields."""
        row = {"kind": "done", "id": "42", "rev": 1, **DONE}
        row.update(kw)
        return row

    def find(self, **kw):
        args = dict(rev=1, incarnation=INC, generation=1, now=NOW)
        args.update(kw)
        return receipts.find(BOARD, "J", "done", "42", **args)

    def enrolled(self) -> Path:
        return paths.host_dir() / "enrolled"


class ReceiptTests(ReceiptCase):
    def test_receipt_round_trips_for_the_same_key_rev_incarnation_and_generation(self):
        r = self.receipt()
        receipts.write(BOARD, r)
        self.assertEqual(self.find(), r)

    def test_missing_record_is_not_found(self):
        receipts.write(BOARD, self.receipt())
        self.assertIsNone(receipts.find(BOARD, "J", "done", "43", rev=1, incarnation=INC, generation=1, now=NOW))

    def test_write_raises_when_the_directory_fsync_fails(self):
        with mock.patch.object(compat, "fsync_dir", side_effect=OSError(5, "EIO")):
            with self.assertRaises(OSError):
                receipts.write(BOARD, self.receipt())

    def test_every_kind_has_decision_fields(self):
        self.assertEqual(set(receipts.DECISION), set(receipts.KINDS))

    def test_args_sha_is_sha256_of_nul_joined_argv_and_is_audit_only(self):
        expected = hashlib.sha256("\0".join(ARGV).encode("utf-8")).hexdigest()
        self.assertEqual(receipts.args_sha(ARGV), expected)
        self.assertNotEqual(receipts.args_sha(ARGV), receipts.args_sha(ARGV[:-1]))
        self.assertIn("audit", receipts.args_sha.__doc__.lower())

    def test_content_sha_is_canonical_over_key_order_and_sensitive_to_values(self):
        reordered = {"group": "g1", "sha": "a" * 40, "artifact": "b@" + "a" * 40}
        self.assertEqual(receipts.content_sha("done", DONE), receipts.content_sha("done", reordered))
        self.assertNotEqual(receipts.content_sha("done", DONE),
                            receipts.content_sha("done", dict(DONE, artifact="b@" + "c" * 40)))
        self.assertRegex(receipts.content_sha("done", DONE), r"[0-9a-f]{64}")

    def test_content_sha_ignores_key_order_of_nested_dicts(self):
        # the land destinations are a nested dict: insertion order must not change the digest
        inner_a = {"state": "pushed", "sha": "a" * 40}
        inner_b = {"sha": "a" * 40, "state": "pushed"}
        d1 = {"destinations": {"https://x.test/a": inner_a, "https://x.test/b": inner_a}}
        d2 = {"destinations": {"https://x.test/b": inner_b, "https://x.test/a": inner_b}}
        self.assertEqual(receipts.content_sha("land", d1), receipts.content_sha("land", d2))
        changed = {"destinations": {"https://x.test/a": dict(inner_a, state="verified"),
                                    "https://x.test/b": inner_a}}
        self.assertNotEqual(receipts.content_sha("land", d1), receipts.content_sha("land", changed))

    def test_content_sha_refuses_unknown_kinds_and_missing_decision_fields(self):
        with self.assertRaises(ValueError):
            receipts.content_sha("bogus", DONE)
        with self.assertRaises(ValueError):
            receipts.content_sha("done", {"artifact": "x"})

    def test_content_sha_refuses_values_that_are_not_canonical_json(self):
        with self.assertRaises(ValueError):
            receipts.content_sha("done", dict(DONE, sha=float("nan")))
        with self.assertRaises(ValueError):
            receipts.content_sha("done", dict(DONE, artifact=object()))


class BindingTests(ReceiptCase):
    def test_matching_row_matches(self):
        # positive control: the untouched row must match, so a constant False would fail here
        self.assertTrue(receipts.matches(self.receipt(), self.row()))

    def test_a_row_whose_decision_fields_were_rewritten_does_not_match(self):
        r = self.receipt()
        self.assertFalse(receipts.matches(r, self.row(artifact="b@" + "e" * 40)))
        self.assertFalse(receipts.matches(r, self.row(sha="e" * 40)))
        self.assertFalse(receipts.matches(r, self.row(group="other")))

    def test_a_verdict_row_with_a_changed_candidate_or_req_does_not_match(self):
        # the verdict's decision fields include candidate, req and artifact (receipts.DECISION)
        verdict = {"verdict": "met", "scope": "goal", "generation": 1,
                   "candidate": "b@" + "a" * 40, "req": "R1", "artifact": None}
        r = self.receipt(kind="verdict", record_id="9", content_sha=receipts.content_sha("verdict", verdict))
        row = dict(verdict, kind="verdict", id="9", rev=1)
        self.assertTrue(receipts.matches(r, row))
        self.assertFalse(receipts.matches(r, dict(row, candidate="b@" + "c" * 40)))
        self.assertFalse(receipts.matches(r, dict(row, req="R2")))
        self.assertFalse(receipts.matches(r, dict(row, req=None)))

    def test_an_artifact_verdict_receipt_binds_its_artifact(self):
        """A receipt for a verdict on one artifact does not match a verdict row for another (or none)."""
        verdict = {"verdict": "met", "scope": "artifact", "generation": 1, "candidate": None, "req": None,
                   "artifact": "feat/a@" + "a" * 40}
        r = self.receipt(kind="verdict", record_id="9", content_sha=receipts.content_sha("verdict", verdict))
        row = dict(verdict, kind="verdict", id="9", rev=1)
        self.assertTrue(receipts.matches(r, row))
        self.assertFalse(receipts.matches(r, dict(row, artifact="feat/b@" + "b" * 40)))
        self.assertFalse(receipts.matches(r, dict(row, artifact=None)))
        with self.assertRaises(ValueError):
            receipts.content_sha("verdict", {k: v for k, v in verdict.items() if k != "artifact"})

    def test_find_latest_ignores_the_revision_find_requires_it(self):
        r = self.receipt(rev=3)
        receipts.write(BOARD, r)
        args = dict(incarnation=INC, generation=1, now=NOW)
        self.assertEqual(receipts.find_latest(BOARD, "J", "done", "42", **args), r)
        self.assertEqual(receipts.find(BOARD, "J", "done", "42", rev=3, **args), r)
        self.assertIsNone(receipts.find(BOARD, "J", "done", "42", rev=2, **args))
        self.assertIsNone(receipts.find_latest(BOARD, "J", "done", "42", incarnation="f" * 32, generation=1,
                                               now=NOW))
        self.assertIsNone(receipts.find_latest(BOARD, "J", "done", "42", incarnation=INC, generation=2, now=NOW))
        self.assertIsNone(receipts.find_latest(BOARD, "J", "done", "42", incarnation=INC, generation=1,
                                               now=NOW + 7200))

    def test_a_row_with_another_kind_or_id_does_not_match(self):
        r = self.receipt()
        self.assertFalse(receipts.matches(r, self.row(kind="verdict")))
        self.assertFalse(receipts.matches(r, self.row(id="43")))

    def test_a_row_missing_a_decision_field_does_not_match(self):
        row = self.row()
        del row["sha"]
        self.assertFalse(receipts.matches(self.receipt(), row))

    def test_a_rev_2_row_is_not_matched_by_a_rev_1_receipt(self):
        r = self.receipt(rev=1)
        self.assertFalse(receipts.matches(r, self.row(rev=2)))
        self.assertTrue(receipts.matches(self.receipt(rev=2), self.row(rev=2)))

    def test_find_requires_the_revision(self):
        receipts.write(BOARD, self.receipt(rev=1))
        self.assertIsNotNone(self.find(rev=1))
        self.assertIsNone(self.find(rev=2))

    def test_a_mutated_row_is_not_matched_even_when_the_receipt_is_found(self):
        receipts.write(BOARD, self.receipt())
        found = self.find()
        self.assertIsNotNone(found)
        self.assertFalse(receipts.matches(found, self.row(artifact="b@" + "f" * 40)))


class ScopeTests(ReceiptCase):
    def test_a_receipt_from_another_incarnation_is_not_found(self):
        receipts.write(BOARD, self.receipt())
        self.assertIsNone(self.find(incarnation="fedcba9876543210fedcba9876543210"))

    def test_a_receipt_from_another_generation_is_not_found(self):
        receipts.write(BOARD, self.receipt(generation=1))
        self.assertIsNone(self.find(generation=2))

    def test_a_receipt_stored_for_another_board_is_not_found(self):
        receipts.write(BOARD, self.receipt())
        planted = self.enrolled() / receipts.receipt_name(BOARD, "J", "done", "42")
        other = receipts.receipt_name("other-board", "J", "done", "42")
        (self.enrolled() / other).write_bytes(planted.read_bytes())
        self.assertIsNone(receipts.find("other-board", "J", "done", "42", rev=1, incarnation=INC,
                                        generation=1, now=NOW))

    def test_an_expired_receipt_is_not_found(self):
        receipts.write(BOARD, self.receipt(expires_at=NOW + 10))
        self.assertIsNotNone(self.find(now=NOW + 9))
        self.assertIsNone(self.find(now=NOW + 10))
        self.assertIsNone(self.find(now=NOW + 1000))

    def test_expiry_must_be_a_finite_number(self):
        for bad in (math.nan, math.inf, "soon", True):
            with self.subTest(expires_at=bad), self.assertRaises(ValueError):
                receipts.write(BOARD, self.receipt(expires_at=bad))

    def test_write_refuses_a_board_key_other_than_the_receipts_and_writes_nothing(self):
        with self.assertRaises(ValueError):
            receipts.write("other", self.receipt())
        self.assertFalse(self.enrolled().exists() and any(self.enrolled().glob("receipt-*")))

    def test_board_key_with_nul_is_refused(self):
        with self.assertRaises(ValueError):
            receipts.write("bk\0x", self.receipt())

    def test_incarnation_must_be_32_hex_digits(self):
        with self.assertRaises(ValueError):
            receipts.write(BOARD, self.receipt(incarnation="not-hex"))


class ValidationTests(ReceiptCase):
    def test_an_invalid_field_is_refused_and_nothing_is_written(self):
        dest = {"https://example.test/r.git": {"state": "pushed", "sha": "a" * 40}}
        bad_dest = {"https://example.test/r.git": {"state": "merged", "sha": "a" * 40}}
        cases = [("kind", dict(kind="bogus")),
                 ("args_sha", dict(args_sha="not-a-digest")),
                 ("content_sha", dict(content_sha="x" * 64)),
                 ("negative generation", dict(generation=-1)),
                 ("bool is not an integer field", dict(attempt=True)),
                 ("control characters in record_id", dict(record_id="42\n43")),
                 ("destination state must be known",
                  dict(kind="land", record_id="7", destinations=bad_dest,
                       content_sha=receipts.content_sha("land", {"destinations": bad_dest}))),
                 ("destinations only on land receipts", dict(kind="done", destinations=dest))]
        cases += [(f"rev {bad!r} is not a positive integer", dict(rev=bad)) for bad in (0, -1, True, "1")]
        for name, kw in cases:
            with self.subTest(name), self.assertRaises(ValueError):
                receipts.write(BOARD, self.receipt(**kw))
        self.assertIsNone(receipts.find(BOARD, "J", "bogus", "42", rev=1, incarnation=INC, generation=1, now=NOW))

    def test_a_receipt_with_a_different_version_is_not_found(self):
        path = receipts.write(BOARD, self.receipt())
        obj = json.loads(path.read_bytes())
        obj["v"] = 2
        path.write_text(json.dumps(obj))
        self.assertIsNone(self.find())

    def test_planted_garbage_receipt_is_not_found(self):
        path = receipts.write(BOARD, self.receipt())
        path.write_text("{not json")
        self.assertIsNone(self.find())

    def test_receipt_planted_under_another_key_is_not_found(self):
        path = receipts.write(BOARD, self.receipt(record_id="43"))
        planted = self.enrolled() / receipts.receipt_name(BOARD, "J", "done", "42")
        planted.write_bytes(path.read_bytes())
        self.assertIsNone(self.find())

    @posix_only("Windows has no O_NOFOLLOW: the link is followed there, see the ledger docstring")
    def test_symlinked_receipt_is_refused_for_being_a_link(self):
        # the link's target is a valid receipt for exactly this key: only the link refusal can fail it
        path = receipts.write(BOARD, self.receipt())
        target = self.home / "target.json"
        target.write_bytes(path.read_bytes())
        os.unlink(path)
        os.symlink(target, path)
        self.assertIsNone(self.find())

    def test_land_receipt_carries_destinations(self):
        dest = {"https://example.test/r.git": {"state": "pushed", "sha": "a" * 40}}
        land = {"destinations": dest}
        r = self.receipt(kind="land", record_id="7", destinations=dest,
                         content_sha=receipts.content_sha("land", land))
        receipts.write(BOARD, r)
        found = receipts.find(BOARD, "J", "land", "7", rev=1, incarnation=INC, generation=1, now=NOW)
        self.assertEqual(found.destinations, dest)


if __name__ == "__main__":
    unittest.main()
