"""The driver's snapshot reader (spec 3.2, 4.0; plan Task 7b) on the memory, file and sqlite boards.

Each control record counts only with a receipt (spec 4.0): receipted vs unreceipted records, receipts of another
generation, expired receipts, and rows whose decision fields no longer match the receipt. The ledger is a real
Ledger value (not saved); receipts are real files under a temporary HOME of the suite sandbox."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import support  # noqa: F401  (puts lib/ on sys.path)
from support import SANDBOX, FileHarness, MemoryHarness, SqliteHarness, home_env
from swarm import receipts
from swarm.supervisor import driver_snapshot as ds
from swarm.supervisor.ledger import Intent, Ledger

BK = "board-key"
INC = "f" * 32
NOW = dt.datetime(2026, 10, 10, 12, 0, tzinfo=dt.timezone.utc)
SHA1 = "1" * 40
SHA2 = "2" * 40
SHA3 = "3" * 40


def verdict_key(artifact: str) -> str:
    return "pipeline.verdict." + hashlib.sha256(artifact.encode()).hexdigest()[:32]


class SnapshotCase:
    """Shared body; each backend's subclass sets harness_factory."""
    harness_factory = None

    @classmethod
    def setUpClass(cls):
        cls.h = cls.harness_factory()

    @classmethod
    def tearDownClass(cls):
        cls.h.close()

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(dir=SANDBOX, prefix="snap-home-"))
        env = mock.patch.dict(os.environ, home_env(self.home))
        env.start()
        self.addCleanup(env.stop)
        self.h.reset()
        self.b = self.h.board()
        self.addCleanup(self.b.close)
        self.b.open_job("J", None, None, None, "me", goal="G")
        self.led = Ledger(board_key=BK, job="J", generation=1, incarnation=INC)

    # ---- helpers
    def read(self, led=None, now=NOW):
        return ds.read(self.b, led or self.led, "J", cfg={}, now=now)

    def receipt(self, kind, record_id, rev, row, *, generation=1, seat="a", expires=None):
        """Write the receipt the hook would write for this record and row (decision fields of `row`)."""
        r = receipts.Receipt(
            board_key=BK, job="J", generation=generation, incarnation=INC, action_id="gen1|fix|a|-|1|-",
            attempt=1, seat=seat, kind=kind, record_id=str(record_id), rev=rev,
            content_sha=receipts.content_sha(kind, row), args_sha=receipts.args_sha(["swarm"]),
            expires_at=(NOW + dt.timedelta(hours=1)).timestamp() if expires is None else expires,
            destinations=row.get("destinations"))
        receipts.write(BK, r)
        return r

    def done(self, artifact, *, receipted=True, generation=1):
        msg = self.b.post("J", "lead", f"DONE {artifact}")
        if receipted:
            self.receipt("done", msg.id, 1, self.done_row(msg.id, artifact), generation=generation)
        return msg.id

    @staticmethod
    def done_row(msg_id, artifact):
        return {"kind": "done", "id": str(msg_id), "rev": 1, "artifact": artifact,
                "sha": artifact.partition("@")[2], "group": artifact.partition("@")[0]}

    def put_verdict(self, artifact, rid, *, verdict="not_met", scope="artifact", generation=1, open_count=2,
                    progress=None, rev=1):
        """A verdict record as verdict_data stores it, carrying its own id and rev."""
        rec = dict(id=rid, rev=rev, artifact=artifact, verdict=verdict, reason="r", next_steps="n", judge="j",
                   at=NOW.isoformat(), scope=scope, generation=generation, open=open_count, progress=progress,
                   candidate=None, req=None)
        self.store_json(verdict_key(artifact), rec)
        return rec

    def store_json(self, key, value):
        body = json.dumps(value, sort_keys=True)
        chunks = [body[i:i + 900] for i in range(0, len(body), 900)] or [""]
        self.b.set_job_data("J", key, str(len(chunks)))
        for i, chunk in enumerate(chunks):
            self.b.set_job_data("J", f"{key}.{i}", chunk)

    def verdict_row(self, rec):
        return {"kind": "verdict", "id": str(rec["id"]), "rev": rec["rev"], "verdict": rec["verdict"],
                "scope": rec["scope"], "generation": rec["generation"], "candidate": rec["candidate"],
                "req": rec["req"]}

    # ---- hand-offs
    def test_receipted_done_is_a_hand_off_attributed_to_its_seat(self):
        mid = self.done(f"feat/a@{SHA1}")
        handoffs = self.read().handoffs
        self.assertEqual([(h.artifact, h.group, h.by, h.receipt_id) for h in handoffs],
                         [(f"feat/a@{SHA1}", "feat/a", "a", mid)])

    def test_unreceipted_done_is_displayed_not_used(self):
        self.done(f"feat/a@{SHA1}", receipted=False)
        self.assertEqual(self.read().handoffs, [])

    def test_a_done_addressed_to_someone_is_not_a_hand_off(self):
        msg = self.b.post("J", "lead", f"DONE feat/a@{SHA1}", to="other")
        self.receipt("done", msg.id, 1, self.done_row(msg.id, f"feat/a@{SHA1}"))
        self.assertEqual(self.read().handoffs, [])

    def test_unreceipted_newer_done_does_not_hide_a_receipted_older_one(self):
        old = self.done(f"feat/a@{SHA1}")
        self.done(f"feat/a@{SHA2}", receipted=False)
        self.assertEqual([(h.artifact, h.receipt_id) for h in self.read().handoffs],
                         [(f"feat/a@{SHA1}", old)])

    def test_done_receipt_of_another_generation_does_not_count(self):
        self.done(f"feat/a@{SHA1}", generation=2)
        self.assertEqual(self.read().handoffs, [])

    def test_expired_receipt_does_not_count(self):
        msg = self.b.post("J", "lead", f"DONE feat/a@{SHA1}")
        self.receipt("done", msg.id, 1, self.done_row(msg.id, f"feat/a@{SHA1}"),
                     expires=(NOW - dt.timedelta(seconds=1)).timestamp())
        self.assertEqual(self.read().handoffs, [])

    def test_receipt_whose_decision_fields_changed_does_not_count(self):
        msg = self.b.post("J", "lead", f"DONE feat/a@{SHA1}")
        self.receipt("done", msg.id, 1, self.done_row(msg.id, f"feat/a@{SHA2}"))   # receipted another sha
        self.assertEqual(self.read().handoffs, [])

    # ---- seats
    def request(self, name, generation=1, **over):
        fields = dict(role="engineer", harness="claude", model="haiku", workdir=f"/w/{name}",
                      brief_path=f"/w/{name}/B.md", requested_by="lead")
        fields.update(over)
        return self.b.request_seat("J", generation, name, **fields)

    def test_unreceipted_seat_request_is_not_admitted(self):
        self.request("a")
        self.assertEqual(self.read().seats, {})

    def test_receipted_seat_request_is_admitted_as_a_seat_view(self):
        st = self.request("a")
        row = {"kind": "seat_request", "id": str(st.id), "rev": st.revision, "role": st.role, "model": st.model,
               "brief": st.brief_path, "workdir": st.workdir}
        self.receipt("seat_request", st.id, st.revision, row)
        seats = self.read().seats
        self.assertEqual(list(seats), ["a"])
        self.assertEqual((seats["a"].state, seats["a"].role, seats["a"].generation), ("requested", "engineer", 1))

    def test_seats_of_another_generation_are_not_read(self):
        st = self.request("b", generation=2)
        row = {"kind": "seat_request", "id": str(st.id), "rev": st.revision, "role": st.role, "model": st.model,
               "brief": st.brief_path, "workdir": st.workdir}
        self.receipt("seat_request", st.id, st.revision, row)
        self.assertEqual(self.read().seats, {})

    # ---- review, verdicts and rounds
    def test_review_is_the_newest_receipted_outcome_per_artifact(self):
        art = f"feat/a@{SHA1}"
        self.done(art)
        for text, rid_ok in ((f"REVIEW {art} CHANGES rep.md", True), (f"REVIEW {art} VERIFIED rep.md", False)):
            msg = self.b.post("J", "rev", text)
            if rid_ok:
                self.receipt("review", msg.id, 1, {"kind": "review", "id": str(msg.id), "rev": 1,
                                                   "group": "feat/a", "artifact": art, "outcome": "changes"})
        self.assertEqual(self.read().reviews, {art: "changes"})

    def test_artifact_verdict_counts_only_when_receipted(self):
        art = f"feat/a@{SHA1}"
        rec = self.put_verdict(art, 77)
        self.assertEqual(self.read().verdicts, {})
        self.receipt("verdict", 77, 1, self.verdict_row(rec))
        vd = self.read().verdicts[art]
        self.assertEqual((vd.verdict, vd.scope, vd.open, vd.receipt_id), ("not_met", "artifact", 2, 77))

    def test_rounds_count_judged_artifacts_per_group_for_this_generation(self):
        for n, art in enumerate((f"feat/a@{SHA1}", f"feat/a@{SHA2}", f"feat/b@{SHA3}"), start=1):
            rec = self.put_verdict(art, 100 + n)
            self.receipt("verdict", rec["id"], 1, self.verdict_row(rec))
        stale = self.put_verdict(f"feat/a@{SHA3}", 200, generation=2)
        self.receipt("verdict", 200, 1, self.verdict_row(stale), generation=2)
        self.assertEqual(self.read().rounds, {"feat/a": 2, "feat/b": 1})

    def test_rounds_ignore_a_receipted_verdict_whose_record_names_another_generation(self):
        rec = self.put_verdict(f"feat/a@{SHA1}", 250, generation=2)
        self.receipt("verdict", 250, 1, self.verdict_row(rec), generation=1)
        self.assertEqual(self.read().rounds, {})

    def test_goal_verdict_needs_its_receipt(self):
        rec = dict(id=300, rev=1, artifact=None, verdict="met", reason="ok", next_steps="", judge="j",
                   at=NOW.isoformat(), scope="goal", generation=1, open=None, progress=None, candidate=None,
                   req=None)
        self.store_json("drive.goal_verdict", rec)
        self.assertIsNone(self.read().goal_verdict)
        self.receipt("verdict", 300, 1, self.verdict_row(rec))
        gv = self.read().goal_verdict
        self.assertEqual((gv.verdict, gv.scope, gv.receipt_id), ("met", "goal", 300))

    # ---- integration and blocking
    def test_finalize_blocked_after_the_newest_hand_off_is_the_blocked_id(self):
        art = f"feat/a@{SHA1}"
        self.done(art)
        msg = self.b.post("J", "int", f"FINALIZE_BLOCKED {art} fix the lint")
        self.receipt("post", msg.id, 1, {"kind": "post", "id": str(msg.id), "rev": 1, "to": None,
                                         "body": f"FINALIZE_BLOCKED {art} fix the lint"})
        block = self.read().integrated["feat/a"]
        self.assertEqual((block["blocked_id"], block["blocked"]),
                         (msg.id, f"FINALIZE_BLOCKED {art} fix the lint"))

    def test_finalize_blocked_before_a_newer_hand_off_is_stale(self):
        art = f"feat/a@{SHA1}"
        msg = self.b.post("J", "int", f"FINALIZE_BLOCKED {art} fix the lint")
        self.receipt("post", msg.id, 1, {"kind": "post", "id": str(msg.id), "rev": 1, "to": None,
                                         "body": f"FINALIZE_BLOCKED {art} fix the lint"})
        self.done(f"feat/a@{SHA2}")
        self.assertIsNone(self.read().integrated.get("feat/a", {}).get("blocked"))

    def test_unreceipted_finalize_blocked_is_ignored(self):
        self.b.post("J", "int", f"FINALIZE_BLOCKED feat/a@{SHA1} fix")
        self.assertEqual(self.read().integrated, {})

    def test_landed_only_from_a_verified_land_receipt(self):
        dest = {"https://example.test/r.git": {"state": "verified", "sha": SHA1}}
        led = Ledger(board_key=BK, job="J", generation=1, incarnation=INC, integrated={
            "feat/a": {"landed": SHA1, "land_id": 7, "land_rev": 1, "destinations": dest}})
        self.receipt("land", 7, 1, {"destinations": dest}, seat=None)
        self.assertEqual(self.read(led).integrated["feat/a"]["landed"], SHA1)
        pushed = {"https://example.test/r.git": {"state": "pushed", "sha": SHA1}}
        led2 = Ledger(board_key=BK, job="J", generation=1, incarnation=INC, integrated={
            "feat/a": {"landed": SHA1, "land_id": 8, "land_rev": 1, "destinations": pushed}})
        self.receipt("land", 8, 1, {"destinations": pushed}, seat=None)
        self.assertIsNone(self.read(led2).integrated["feat/a"]["landed"])

    def test_verified_destination_at_another_sha_is_not_a_landing(self):
        dest = {"https://example.test/r.git": {"state": "verified", "sha": SHA2}}
        led = Ledger(board_key=BK, job="J", generation=1, incarnation=INC, integrated={
            "feat/a": {"landed": SHA1, "land_id": 9, "land_rev": 1, "destinations": dest}})
        self.receipt("land", 9, 1, {"destinations": dest}, seat=None)
        self.assertIsNone(self.read(led).integrated["feat/a"]["landed"])

    # ---- questions and goal-ready
    def test_question_counts_only_when_receipted(self):
        from datetime import timedelta
        until = NOW + timedelta(hours=4)
        bl = self.b.open_blocker("J", "ask", "lead", "which way?", until=until, default_value="left",
                                 seat="a", generation=1)
        self.assertEqual(self.read().questions, {})
        self.receipt("ask", bl.id, 1, {"kind": "ask", "id": str(bl.id), "rev": 1, "question": "which way?",
                                       "until": until.isoformat(), "default": "left"})
        (q,) = self.read().questions["a"]
        self.assertEqual((q.text, q.default, q.receipt_id, q.generation), ("which way?", "left", bl.id, 1))

    def test_goal_ready_carries_id_req_candidate_at_and_added_requirements(self):
        gr = dict(id=401, rev=1, generation=1, candidate=SHA1, req="sha256:abc", at=NOW.isoformat(),
                  added_requirements=["docs/x.md@" + SHA2])
        self.store_json("drive.goal_ready", gr)
        self.assertIsNone(self.read().goal_ready)
        self.receipt("goal_ready", 401, 1, {"kind": "goal_ready", "id": "401", "rev": 1, "generation": 1,
                                            "candidate": SHA1})
        self.assertEqual(self.read().goal_ready, {"id": 401, "req": "sha256:abc", "candidate": SHA1,
                                                  "at": NOW.isoformat(), "added_requirements": ["docs/x.md@" + SHA2]})

    # ---- the ledger's own state
    def test_live_seats_are_those_with_a_claiming_or_claimed_intent(self):
        self.led.intents = [
            Intent("L1", "gen1|seat|a|-|1|-", 1, "a", 1.0, 1.0, [], "claimed"),
            Intent("L2", "gen1|seat|b|-|1|-", 1, "b", 1.0, 1.0, [], "claiming"),
            Intent("L3", "gen1|seat|c|-|1|-", 1, "c", 1.0, 1.0, [], "concluded", "completed", True, 0.5),
        ]
        self.assertEqual(self.read().live, {"a", "b"})

    def test_halted_follows_the_ledger_and_status_the_board(self):
        self.assertFalse(self.read().halted)
        self.assertEqual(self.read().status, "active")
        self.led.halted = {"at": NOW.isoformat(), "by": "person"}
        self.assertTrue(self.read().halted)

    def test_drive_cfg_takes_the_recipe_from_the_owner_record_never_config(self):
        cfg = ds.drive_cfg(BK, "J", {"recipe": "artifact", "review_role": None, "max_rounds": 5})
        self.assertEqual((cfg["recipe"], cfg["review_role"]), ("coding", "reviewer"))
        self.assertEqual(cfg["max_rounds"], 5)


class MemorySnapshot(SnapshotCase, unittest.TestCase):
    harness_factory = staticmethod(lambda: MemoryHarness())


class FileSnapshot(SnapshotCase, unittest.TestCase):
    harness_factory = staticmethod(lambda: FileHarness())


class SqliteSnapshot(SnapshotCase, unittest.TestCase):
    harness_factory = staticmethod(lambda: SqliteHarness())
