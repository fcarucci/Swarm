"""review.verdict_data binds a verdict to its VERDICT message and to what it judges (Task 6b carrier contract):
an artifact verdict to its artifact, a goal or ruling verdict to its candidate, both under the message id."""
import datetime as dt
import json
import unittest

from support import MemoryHarness  # noqa: F401 (puts lib/ on sys.path)

from swarm.review import artifact_verdicts, verdict_data

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

    def test_message_id_is_null_when_not_given(self):
        raw = verdict_data(None, "feat/x@abc1234", "not_met", "no", "fix it", "Judge", NOW, open_count=1)
        self.assertIsNone(artifact_verdicts(_Board(raw), "J")["feat/x@abc1234"]["message_id"])


class GoalBinding(unittest.TestCase):
    def test_goal_verdict_is_bound_to_its_candidate_and_message_under_its_generation(self):
        raw = verdict_data(None, None, "met", "all held", None, "Judge", NOW, scope="goal", generation=2,
                           candidate="feat/x@abc1234", req="r" * 64, message_id=88)
        data = json.loads(raw)
        rec = _chunked(data, "drive.verdict.2.goal")
        self.assertEqual((rec["verdict"], rec["scope"], rec["generation"]), ("met", "goal", 2))
        self.assertEqual((rec["candidate"], rec["req"], rec["message_id"]), ("feat/x@abc1234", "r" * 64, 88))
        self.assertIsNone(rec["artifact"])

    def test_goal_records_are_not_artifact_verdicts(self):
        raw = verdict_data(None, None, "met", "all held", None, "Judge", NOW, scope="goal", generation=2,
                           candidate="feat/x@abc1234", req="r" * 64, message_id=88)
        self.assertEqual(artifact_verdicts(_Board(raw), "J"), {})

    def test_ruling_verdict_is_keyed_by_its_own_scope(self):
        raw = verdict_data(None, None, "unreachable", "cannot", None, "Judge", NOW, scope="ruling", generation=3,
                           candidate="report:r.md@" + "a" * 40, req="s" * 64, message_id=91)
        data = json.loads(raw)
        rec = _chunked(data, "drive.verdict.3.ruling")
        self.assertEqual((rec["candidate"], rec["message_id"]), ("report:r.md@" + "a" * 40, 91))
        self.assertNotIn("drive.verdict.3.goal", data)

    def test_goal_verdict_without_generation_has_no_drive_record(self):
        raw = verdict_data(None, None, "met", "all held", None, "Judge", NOW, scope="goal",
                           candidate="feat/x@abc1234", message_id=5)
        self.assertFalse([k for k in json.loads(raw) if k.startswith("drive.verdict.")])


if __name__ == "__main__":
    unittest.main()
