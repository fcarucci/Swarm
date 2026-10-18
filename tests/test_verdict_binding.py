"""review.verdict_data binds a verdict to its VERDICT message and to what it judges (Task 6b carrier contract):
an artifact verdict to its artifact, a goal or ruling verdict to its candidate, both under the message id."""
import datetime as dt
import json
import unittest

from support import MemoryHarness  # noqa: F401 (puts lib/ on sys.path)

from swarm.review import (artifact_verdicts, bound_verdict, verdict_data,
                          verdict_text)

NOW = dt.datetime(2026, 10, 11, 9, 0, tzinfo=dt.timezone.utc)


class _Board:
    """The one board read verdict_data's readers make: the job's data."""
    def __init__(self, raw):
        self.raw = raw

    def job_data(self, job):
        return json.loads(self.raw)


def _chunked(data, key):
    return json.loads("".join(data[f"{key}.{i}"] for i in range(int(data[key]))))


class ArtifactBinding(unittest.TestCase):
    def test_artifact_verdict_stores_its_message_id_with_the_artifact(self):
        raw = verdict_data(None, "feat/x@abc1234", "met", "ok", None, "Judge", NOW, message_id=77)
        rec = artifact_verdicts(_Board(raw), "J")["feat/x@abc1234"]
        self.assertEqual((rec["artifact"], rec["verdict"], rec["message_id"]), ("feat/x@abc1234", "met", 77))

    def test_an_unbound_record_is_not_an_artifact_verdict(self):
        """The record before its binding write (message_id None) counts for no reader."""
        raw = verdict_data(None, "feat/x@abc1234", "not_met", "no", "fix it", "Judge", NOW, open_count=1,
                           message_id=0)
        rec = _chunked(json.loads(raw), next(k for k in json.loads(raw) if k.startswith("pipeline.verdict.")
                                             and k.count(".") == 2))
        self.assertEqual(rec["message_id"], 0)
        self.assertEqual(artifact_verdicts(_Board(raw), "J"), {})

    def test_a_legacy_record_without_message_id_still_counts_for_artifacts(self):
        raw = verdict_data(None, "feat/x@abc1234", "met", "ok", None, "Judge", NOW, message_id=9)
        data = json.loads(raw)
        key = next(k for k in data if k.startswith("pipeline.verdict.") and k.count(".") == 2)
        rec = _chunked(data, key)
        del rec["message_id"]
        data[key + ".0"] = json.dumps(rec)
        self.assertIn("feat/x@abc1234", artifact_verdicts(_Board(json.dumps(data)), "J"))


class BoundVerdict(unittest.TestCase):
    def test_only_a_positive_int_message_id_binds(self):
        for mid, ok in ((5, True), (None, False), (0, False), (-1, False), ("5", False), (True, False)):
            self.assertEqual(bound_verdict({"message_id": mid}) is not None, ok, mid)

    def test_a_record_without_message_id_needs_legacy_ok(self):
        self.assertIsNone(bound_verdict({"verdict": "met"}))
        self.assertIsNotNone(bound_verdict({"verdict": "met"}, legacy_ok=True))
        self.assertIsNone(bound_verdict({"message_id": None}, legacy_ok=True))
        self.assertIsNone(bound_verdict(None))


class GoalBinding(unittest.TestCase):
    def test_goal_records_are_not_artifact_verdicts(self):
        raw = verdict_data(None, None, "met", "all held", None, "Judge", NOW, scope="goal", generation=2,
                           candidate="feat/x@abc1234", req="r" * 64, message_id=88)
        self.assertEqual(artifact_verdicts(_Board(raw), "J"), {})

    def test_the_verdict_text_names_what_a_goal_verdict_judges(self):
        self.assertEqual(verdict_text("met", "ok"), "VERDICT met: ok")
        self.assertEqual(verdict_text("met", "ok", scope="goal", generation=2, candidate="c@1234567", req="r"),
                         "VERDICT met [goal gen=2 candidate=c@1234567 req=r]: ok")

    def test_goal_verdict_has_no_record(self):
        raw = verdict_data(None, None, "met", "all held", None, "Judge", NOW, scope="goal",
                           candidate="feat/x@abc1234", message_id=5)
        self.assertFalse([k for k in json.loads(raw) if k.startswith("pipeline.verdict.")])


if __name__ == "__main__":
    unittest.main()
