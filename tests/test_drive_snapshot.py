"""The driver's snapshot reader (spec 3.2, 4.0; plan Task 7b) on the memory, file and sqlite boards.

Each control record counts only with a receipt (spec 4.0): receipted vs unreceipted records, receipts of another
generation, expired receipts, and rows whose decision fields no longer match the receipt. Verdicts and goal-ready
are bound to their board messages; the requirements digest is checked against the ledger's pins. The ledger is a
real Ledger value (not saved); receipts are real files under a temporary HOME of the suite sandbox."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import support  # noqa: F401  (puts lib/ on sys.path)
from support import SANDBOX, add_backend_variants, drive_backends, home_env
from swarm import receipts, review
from swarm.supervisor import driver_snapshot as ds
from swarm.supervisor.drive_authz import requirements_digest
from swarm.supervisor.ledger import Intent, Ledger

BK = "board-key"
INC = "f" * 32
NOW = dt.datetime(2026, 10, 10, 12, 0, tzinfo=dt.timezone.utc)
SHA1 = "1" * 40
SHA2 = "2" * 40
SHA3 = "3" * 40
PINS = {"/w/req.md": "b" * 40}


def verdict_key(artifact: str) -> str:
    return "pipeline.verdict." + hashlib.sha256(artifact.encode()).hexdigest()[:32]


def goal_key(rec, scope="goal", generation=1):
    """Where review.verdict_data stores a goal or ruling verdict: one key per (generation, scope, candidate, req)."""
    return review.drive_verdict_key(generation, scope, rec.get("candidate"), rec.get("req"))

class SnapshotCase:
    """Shared body; each backend's subclass sets harness_factory."""
    harness_factory = None

    @classmethod
    def setUpClass(cls):
        cls.h = cls.harness_factory(cls.__qualname__)

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

    def receipt(self, kind, record_id, rev, row, *, generation=1, launch="a", expires=None):
        """Write the receipt the hook would write for this record and row (decision fields of `row`)."""
        r = receipts.Receipt(
            board_key=BK, job="J", generation=generation, incarnation=INC, action_id="gen1|fix|a|-|1|-",
            attempt=1, launch=launch, kind=kind, record_id=str(record_id), rev=rev,
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

    def store_json(self, key, value):
        body = json.dumps(value, sort_keys=True)
        chunks = [body[i:i + 900] for i in range(0, len(body), 900)] or [""]
        self.b.set_job_data("J", key, str(len(chunks)))
        for i, chunk in enumerate(chunks):
            self.b.set_job_data("J", f"{key}.{i}", chunk)

    def publish(self, rec, *, receipted=True, generation=1, launch="j", text=None, addressed=None):
        """Post a VERDICT message for `rec`, store its id in rec as message_id, and write the receipt the hook would
        write for it (the artifact, or the candidate of a goal or ruling verdict, is part of the row)."""
        msg = self.b.post("J", "judge", text or f"VERDICT {rec['verdict']}: {rec['reason']}", to=addressed)
        rec["message_id"] = msg.id
        if receipted:
            row = {"kind": "verdict", "id": str(msg.id), "rev": 1, "verdict": rec["verdict"],
                   "scope": rec["scope"], "generation": rec["generation"], "candidate": rec["candidate"],
                   "req": rec["req"],
                   "artifact": rec["artifact"] if rec["scope"] == "artifact" else None}
            self.receipt("verdict", msg.id, 1, row, generation=generation, launch=launch)
        return msg.id

    @staticmethod
    def record(artifact=None, *, verdict="not_met", scope="artifact", generation=1, open_count=2,
               reason="reason", candidate=None, req=None, progress=None):
        return dict(artifact=artifact, verdict=verdict, scope=scope, generation=generation, open=open_count,
                    reason=reason, next_steps="next", judge="judge", at=NOW.isoformat(), progress=progress,
                    candidate=candidate, req=req)

    def artifact_verdict(self, artifact, *, receipted=True, generation=1, **kw):
        """Publish an artifact verdict and store its record (job data) with the message id."""
        rec = self.record(artifact, generation=generation, **kw)
        self.publish(rec, receipted=receipted, generation=generation)
        self.store_json(verdict_key(artifact), rec)
        return rec

    @staticmethod
    def requirements_digest(pins):
        return requirements_digest(pins)

    # ---- hand-offs
    def test_receipted_done_is_a_hand_off_attributed_to_its_launch(self):
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

    # ---- launches
    def request(self, name, generation=1, **over):
        fields = dict(role="engineer", harness="claude", model="haiku", workdir=f"/w/{name}",
                      brief=f"/w/{name}/B.md", queued_by="lead")
        fields.update(over)
        return self.b.queue_launch("J", generation, name, **fields)





    def test_launches_of_another_generation_are_not_read(self):
        st = self.request("b", generation=2)
        self.assertEqual(self.read().launches, {})

    # ---- review
    def review(self, artifact, outcome, *, receipted=True):
        msg = self.b.post("J", "rev", f"REVIEW {artifact} {outcome} rep.md")
        if receipted:
            self.receipt("review", msg.id, 1, {"kind": "review", "id": str(msg.id), "rev": 1,
                                               "group": artifact.partition("@")[0], "artifact": artifact,
                                               "outcome": outcome.lower()})

    def test_review_outcome_is_the_newest_receipted_one(self):
        art = f"feat/a@{SHA1}"
        self.review(art, "CHANGES")
        self.review(art, "VERIFIED", receipted=False)
        self.assertEqual(self.read().reviews, {art: "changes"})

    def test_review_ordering_newer_verified_wins(self):
        art = f"feat/a@{SHA1}"
        self.review(art, "CHANGES")
        self.review(art, "VERIFIED")
        self.assertEqual(self.read().reviews, {art: "verified"})

    def test_review_ordering_newer_changes_wins(self):
        art = f"feat/a@{SHA1}"
        self.review(art, "VERIFIED")
        self.review(art, "CHANGES")
        self.assertEqual(self.read().reviews, {art: "changes"})

    # ---- verdicts (artifact, goal, ruling) and rounds: bound by message_id and the receipt
    def test_artifact_verdict_counts_when_its_message_id_and_receipt_bind_it(self):
        art = f"feat/a@{SHA1}"
        rec = self.artifact_verdict(art)
        vd = self.read().verdicts[art]
        self.assertEqual((vd.verdict, vd.scope, vd.open, vd.receipt_id), ("not_met", "artifact", 2, rec["message_id"]))

    def test_verdict_message_without_receipt_does_not_count(self):
        self.artifact_verdict(f"feat/a@{SHA1}", receipted=False)
        self.assertEqual(self.read().verdicts, {})

    def test_a_record_without_message_id_binds_nothing(self):
        art = f"feat/a@{SHA1}"
        rec = self.artifact_verdict(art)
        del rec["message_id"]
        self.store_json(verdict_key(art), rec)
        self.assertEqual(self.read().verdicts, {})

    def test_text_is_not_what_binds_a_record(self):
        # the message reads differently from the record's reason; the receipt for its id still binds it
        art = f"feat/a@{SHA1}"
        rec = self.record(art)
        self.publish(rec, text="VERDICT not_met: a different wording")
        self.store_json(verdict_key(art), rec)
        self.assertIn(art, self.read().verdicts)

    def test_a_record_cannot_borrow_another_artifacts_receipted_message(self):
        art_y = f"feat/y@{SHA2}"
        rec_y = self.record(art_y)
        self.publish(rec_y)                                  # Y's verdict, receipted for Y
        art_x = f"feat/x@{SHA1}"
        forged = self.record(art_x, reason="same wording")
        forged["message_id"] = rec_y["message_id"]           # X's record points at Y's message
        self.store_json(verdict_key(art_x), forged)
        self.store_json(verdict_key(art_y), rec_y)
        self.assertEqual(set(self.read().verdicts), {art_y})

    def test_a_receipt_for_another_verdict_does_not_bind_the_record(self):
        art = f"feat/a@{SHA1}"
        rec = self.record(art, verdict="not_met")
        met = dict(rec, verdict="met")
        self.publish(met)                                    # the receipt is for a met verdict
        rec["message_id"] = met["message_id"]                # the record names that message as not_met
        self.store_json(verdict_key(art), rec)
        self.assertEqual(self.read().verdicts, {})

    def test_an_addressed_verdict_message_does_not_bind(self):
        art = f"feat/a@{SHA1}"
        rec = self.record(art)
        self.publish(rec, addressed="other")
        self.store_json(verdict_key(art), rec)
        self.assertEqual(self.read().verdicts, {})

    def test_a_message_that_is_not_a_verdict_does_not_bind(self):
        art = f"feat/a@{SHA1}"
        rec = self.record(art)
        msg = self.b.post("J", "judge", f"DONE {art}")
        rec["message_id"] = msg.id
        self.receipt("verdict", msg.id, 1, {"kind": "verdict", "id": str(msg.id), "rev": 1, "verdict": "not_met",
                                            "scope": "artifact", "generation": 1, "candidate": None, "req": None,
                                            "artifact": art})
        self.store_json(verdict_key(art), rec)
        self.assertEqual(self.read().verdicts, {})

    def task5_receipt(self, mid, ns, rec):
        """A verdict receipt built exactly as Task 5 builds it: drivegate._verdict_decision hashed over the
        command's own namespace (only its message check is stubbed: the message is posted here, first)."""
        from swarm import drivegate
        ctx = SimpleNamespace(job="J", led=SimpleNamespace(generation=1))
        with mock.patch("swarm.drivegate._expect_message"):
            decision = drivegate._verdict_decision(None, ctx, ns, mid, None)
        row = dict(decision, kind="verdict", id=str(mid), rev=1)
        self.receipt("verdict", mid, 1, row)
        rec.update(decision)
        rec["message_id"] = mid
        return decision

    def test_a_goal_verdict_receipted_as_task5_builds_it_binds(self):
        # M1: a goal verdict's receipt carries artifact None and the candidate; the reader must hash the same way
        mid = self.b.post("J", "judge", "VERDICT met: ok").id
        ns = SimpleNamespace(verdict="met", scope="goal", artifact=None, candidate=SHA1, req=None,
                             reason=["ok"], reason_opt=None)
        rec = self.record(None, verdict="met", scope="goal", open_count=None, candidate=SHA1, reason="ok")
        decision = self.task5_receipt(mid, ns, rec)
        self.assertIsNone(decision["artifact"])
        self.store_json(goal_key(rec), rec)
        gv = self.read().goal_verdict
        self.assertEqual((gv.verdict, gv.candidate, gv.receipt_id), ("met", SHA1, mid))

    def test_an_artifact_verdict_receipted_as_task5_builds_it_binds(self):
        art = f"feat/a@{SHA1}"
        mid = self.b.post("J", "judge", "VERDICT not_met: why").id
        ns = SimpleNamespace(verdict="not_met", scope="artifact", artifact=art, candidate=None, req=None,
                             reason=["why"], reason_opt=None)
        rec = self.record(art, reason="why")
        decision = self.task5_receipt(mid, ns, rec)
        self.assertEqual(decision["artifact"], art)
        self.store_json(verdict_key(art), rec)
        self.assertEqual(self.read().verdicts[art].receipt_id, mid)

    def test_a_goal_record_naming_an_artifact_binds_by_candidate_and_carries_no_artifact(self):
        # the receipt binds artifact None and the candidate, as Task 5 writes it; a stray artifact field on the
        # stored goal record is not part of the binding and is not carried into the view
        mid = self.b.post("J", "judge", "VERDICT met: ok").id
        ns = SimpleNamespace(verdict="met", scope="goal", artifact=None, candidate=SHA1, req=None,
                             reason=["ok"], reason_opt=None)
        rec = self.record(None, verdict="met", scope="goal", open_count=None, candidate=SHA1, reason="ok")
        self.task5_receipt(mid, ns, rec)
        self.store_json(goal_key(dict(rec, artifact=SHA1)), dict(rec, artifact=SHA1))
        gv = self.read().goal_verdict
        self.assertEqual((gv.receipt_id, gv.artifact), (mid, None))



    def test_rounds_count_judged_artifacts_per_group_for_this_generation(self):
        for art in (f"feat/a@{SHA1}", f"feat/a@{SHA2}", f"feat/b@{SHA3}"):
            self.artifact_verdict(art)
        self.artifact_verdict(f"feat/a@{SHA3}", generation=2)
        self.assertEqual(self.read().rounds, {"feat/a": 2, "feat/b": 1})

    def test_rounds_ignore_a_receipted_verdict_whose_record_names_another_generation(self):
        rec = self.record(f"feat/a@{SHA1}", generation=2)
        self.publish(rec, generation=1)                      # receipted at generation 1, record says 2
        self.store_json(verdict_key(f"feat/a@{SHA1}"), rec)
        self.assertEqual(self.read().rounds, {})

    def test_met_landed_and_withdrawn_groups_are_not_in_flight(self):
        self.artifact_verdict(f"feat/a@{SHA1}", verdict="met", open_count=None)
        self.artifact_verdict(f"feat/b@{SHA2}")
        self.artifact_verdict(f"feat/c@{SHA3}")
        led = Ledger(board_key=BK, job="J", generation=1, incarnation=INC,
                     integrated={"feat/c": {"withdrawn": "dropped"}})
        self.assertEqual(self.read(led).rounds, {"feat/b": 1})

    def test_goal_verdict_is_bound_by_its_candidate(self):
        rec = self.record(None, verdict="met", scope="goal", open_count=None, candidate=SHA1)
        self.publish(rec)
        self.store_json(goal_key(rec), rec)
        gv = self.read().goal_verdict
        self.assertEqual((gv.verdict, gv.scope, gv.receipt_id), ("met", "goal", rec["message_id"]))

    def test_goal_verdict_as_the_verdict_cli_stores_it_binds(self):
        """QA live-met: review.verdict_data stores a goal verdict under drive.verdict.<gen>.goal.<digest>; the
        snapshot read drive.verdict.<gen>.goal and never saw it, so a met goal never closed the job."""
        import datetime as _dt
        rec = self.record(None, verdict="met", scope="goal", open_count=None, candidate=SHA1)
        self.publish(rec)
        raw = review.verdict_data(None, None, "met", rec.get("reason", "ok"), None, "judge",
                                  _dt.datetime.now(_dt.timezone.utc), scope="goal", generation=1,
                                  candidate=SHA1, req=rec.get("req"), message_id=rec["message_id"])
        for k, v in json.loads(raw).items():
            if k.startswith("drive.verdict."):
                self.b.set_job_data("J", k, v)
        self.assertEqual(self.read().goal_verdict.receipt_id, rec["message_id"])

    def test_a_recorded_unreachable_ruling_closes_the_job_and_launches_nothing(self):
        """QA release blocker: a ruling verdict stored by the verdict CLI never terminalized the job, so launches kept
        relaunching. Read as stored, it is the goal verdict, and the next actions are the close alone."""
        import datetime as _dt
        from swarm.supervisor import driver_model as _dm
        rec = self.record(None, verdict="unreachable", scope="ruling", open_count=None, candidate=SHA1)
        self.publish(rec)
        raw = review.verdict_data(None, None, "unreachable", rec.get("reason", "no"), None, "judge",
                                  _dt.datetime.now(_dt.timezone.utc), scope="ruling", generation=1,
                                  candidate=SHA1, req=rec.get("req"), message_id=rec["message_id"])
        for k, v in json.loads(raw).items():
            if k.startswith("drive.verdict."):
                self.b.set_job_data("J", k, v)
        snap = self.read()
        self.assertEqual((snap.goal_verdict.verdict, snap.goal_verdict.scope), ("unreachable", "ruling"))
        acts = _dm.next_actions(snap, {})
        self.assertEqual([(a.kind, a.reason) for a in acts], [("close", "unreachable")])

    def test_goal_verdict_under_another_records_key_is_refused(self):
        rec = self.record(None, verdict="met", scope="goal", open_count=None, candidate=SHA1)
        self.publish(rec)
        self.store_json(goal_key(dict(rec, candidate=SHA2)), rec)
        self.assertIsNone(self.read().goal_verdict)

    def test_goal_verdict_whose_candidate_changed_is_refused(self):
        rec = self.record(None, verdict="met", scope="goal", open_count=None, candidate=SHA1)
        self.publish(rec)
        rec["candidate"] = SHA2
        self.store_json(goal_key(rec), rec)
        self.assertIsNone(self.read().goal_verdict)

    def test_goal_verdict_without_a_candidate_is_refused(self):
        rec = self.record(None, verdict="met", scope="goal", open_count=None, candidate=None)
        self.publish(rec)
        self.store_json(goal_key(rec), rec)
        self.assertIsNone(self.read().goal_verdict)

    def test_goal_key_refuses_a_record_of_another_scope(self):
        # an artifact verdict, validly receipted for its artifact, stored under the goal key: not a goal verdict
        rec = self.record(f"feat/a@{SHA1}", verdict="met", scope="artifact", open_count=None)
        self.publish(rec)
        self.store_json(goal_key(rec), rec)
        self.assertIsNone(self.read().goal_verdict)

    def test_goal_verdict_of_another_generation_is_not_read(self):
        rec = self.record(None, verdict="met", scope="goal", generation=2, open_count=None, candidate=SHA1)
        self.publish(rec, generation=2)
        self.store_json(goal_key(rec), rec)
        self.assertIsNone(self.read().goal_verdict)

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
        self.receipt("land", 7, 1, {"destinations": dest}, launch=None)
        self.assertEqual(self.read(led).integrated["feat/a"]["landed"], SHA1)
        pushed = {"https://example.test/r.git": {"state": "pushed", "sha": SHA1}}
        led2 = Ledger(board_key=BK, job="J", generation=1, incarnation=INC, integrated={
            "feat/a": {"landed": SHA1, "land_id": 8, "land_rev": 1, "destinations": pushed}})
        self.receipt("land", 8, 1, {"destinations": pushed}, launch=None)
        self.assertIsNone(self.read(led2).integrated["feat/a"]["landed"])

    def test_landed_from_the_entry_swarm_land_writes(self):
        """QA live-met: swarm.land's _finish records land_id as the request's id in text and no destinations;
        the snapshot read nothing from it, so a landed group was integrated again and again."""
        dest = {"https://example.test/r.git": {"state": "verified", "sha": SHA1}}
        led = Ledger(board_key=BK, job="J", generation=1, incarnation=INC, integrated={
            "feat/a": {"landed": SHA1, "land_id": "25", "land_rev": 3}})
        self.receipt("land", 25, 3, {"destinations": dest}, launch=None)
        self.assertEqual(self.read(led).integrated["feat/a"]["landed"], SHA1)
        for lid in ("x25", "", None, 2.5):
            with self.subTest(land_id=lid):
                bad = Ledger(board_key=BK, job="J", generation=1, incarnation=INC, integrated={
                    "feat/a": {"landed": SHA1, "land_id": lid, "land_rev": 3}})
                self.assertIsNone(self.read(bad).integrated["feat/a"]["landed"])

    def test_a_land_receipt_whose_digest_does_not_bind_its_destinations_lands_nothing(self):
        dest = {"https://example.test/r.git": {"state": "verified", "sha": SHA1}}
        r = receipts.Receipt(
            board_key=BK, job="J", generation=1, incarnation=INC, action_id="a", attempt=1, launch=None, kind="land",
            record_id="26", rev=1, content_sha=receipts.content_sha("land", {"destinations": {}}),
            args_sha=receipts.args_sha(["swarm"]), expires_at=(NOW + dt.timedelta(hours=1)).timestamp(),
            destinations=dest)
        receipts.write(BK, r)
        led = Ledger(board_key=BK, job="J", generation=1, incarnation=INC, integrated={
            "feat/a": {"landed": SHA1, "land_id": "26", "land_rev": 1}})
        self.assertIsNone(self.read(led).integrated["feat/a"]["landed"])

    def test_landed_needs_every_destination_verified_not_just_one(self):
        dest = {"https://example.test/a.git": {"state": "verified", "sha": SHA1},
                "https://example.test/b.git": {"state": "pushed", "sha": SHA1}}
        led = Ledger(board_key=BK, job="J", generation=1, incarnation=INC, integrated={
            "feat/a": {"landed": SHA1, "land_id": 9, "land_rev": 1, "destinations": dest}})
        self.receipt("land", 9, 1, {"destinations": dest}, launch=None)
        self.assertIsNone(self.read(led).integrated["feat/a"]["landed"])

    def test_landed_needs_the_destination_sha_to_be_the_landed_sha(self):
        dest = {"https://example.test/a.git": {"state": "verified", "sha": SHA2}}
        led = Ledger(board_key=BK, job="J", generation=1, incarnation=INC, integrated={
            "feat/a": {"landed": SHA1, "land_id": 10, "land_rev": 1, "destinations": dest}})
        self.receipt("land", 10, 1, {"destinations": dest}, launch=None)
        self.assertIsNone(self.read(led).integrated["feat/a"]["landed"])

    # ---- questions and goal-ready
    def test_question_counts_only_when_receipted(self):
        until = NOW + dt.timedelta(hours=4)
        bl = self.b.open_blocker("J", "ask", "lead", "which way?", until=until, default_value="left",
                                 launch="a", generation=1)
        self.assertEqual(self.read().questions, {})
        self.receipt("ask", bl.id, 1, {"kind": "ask", "id": str(bl.id), "rev": 1, "question": "which way?",
                                       "until": until.isoformat(), "default": "left"})
        (q,) = self.read().questions["a"]
        self.assertEqual((q.text, q.default, q.receipt_id, q.generation), ("which way?", "left", bl.id, 1))

    def test_question_whose_blocker_names_another_generation_is_not_read(self):
        bl = self.b.open_blocker("J", "ask", "lead", "which way?", launch="a", generation=2)
        self.receipt("ask", bl.id, 1, {"kind": "ask", "id": str(bl.id), "rev": 1, "question": "which way?",
                                       "until": None, "default": None}, generation=1)
        self.assertEqual(self.read().questions, {})

    def test_question_until_in_another_zone_still_matches_its_receipt(self):
        # the receipt stores the UTC instant; a database session in UTC+2 hands back the same instant with +02:00
        until_utc = NOW + dt.timedelta(hours=4)
        zone = dt.timezone(dt.timedelta(hours=2))
        bl = self.b.open_blocker("J", "ask", "lead", "which way?", until=until_utc.astimezone(zone),
                                 default_value="left", launch="a", generation=1)
        self.receipt("ask", bl.id, 1, {"kind": "ask", "id": str(bl.id), "rev": 1, "question": "which way?",
                                       "until": until_utc.isoformat(), "default": "left"})
        self.assertEqual(len(self.read().questions.get("a", [])), 1)

    def test_question_of_another_generation_is_not_read(self):
        bl = self.b.open_blocker("J", "ask", "lead", "which way?", launch="a", generation=2)
        self.receipt("ask", bl.id, 1, {"kind": "ask", "id": str(bl.id), "rev": 1, "question": "which way?",
                                       "until": None, "default": None}, generation=2)
        self.assertEqual(self.read().questions, {})

    def goal_ready(self, candidate=SHA1, *, pins=PINS, req=None, receipted=True, generation=1):
        """A GOAL-READY message as drive_cli posts it, its receipt, and the ledger's pins."""
        self.led.pinned_requirements = dict(pins)
        digest = req or self.requirements_digest(pins)
        msg = self.b.post("J", "lead", f"GOAL-READY {candidate} req={digest}")
        if receipted:
            self.receipt("goal_ready", msg.id, 1, {"kind": "goal_ready", "id": str(msg.id), "rev": 1,
                                                   "generation": generation, "candidate": candidate},
                         generation=generation)
        return msg.id

    def test_goal_ready_carries_id_req_candidate_at_and_added_requirements_from_the_ledger(self):
        mid = self.goal_ready()
        gr = self.read().goal_ready
        self.assertEqual(gr["id"], mid)
        self.assertEqual(gr["req"], self.requirements_digest(PINS))
        self.assertEqual((gr["candidate"], gr["added_requirements"]), (SHA1, ["/w/req.md@" + "b" * 40]))
        self.assertIsInstance(gr["at"], str)

    def test_goal_ready_without_its_receipt_is_ignored(self):
        self.goal_ready(receipted=False)
        self.assertIsNone(self.read().goal_ready)

    def test_goal_ready_whose_req_is_not_the_ledger_digest_is_ignored(self):
        self.goal_ready(req="0" * 64)
        self.assertIsNone(self.read().goal_ready)

    def test_goal_ready_without_pinned_requirements_is_ignored(self):
        self.goal_ready(pins={})
        self.assertIsNone(self.read().goal_ready)

    def test_an_addressed_goal_ready_is_not_a_goal_ready(self):
        self.led.pinned_requirements = dict(PINS)
        msg = self.b.post("J", "lead", f"GOAL-READY {SHA1} req={self.requirements_digest(PINS)}", to="other")
        self.receipt("goal_ready", msg.id, 1, {"kind": "goal_ready", "id": str(msg.id), "rev": 1,
                                               "generation": 1, "candidate": SHA1})
        self.assertIsNone(self.read().goal_ready)

    def test_goal_ready_of_another_generation_is_ignored(self):
        self.goal_ready(generation=2)
        self.assertIsNone(self.read().goal_ready)

    def test_goal_ready_is_read_for_the_ledgers_generation(self):
        led2 = Ledger(board_key=BK, job="J", generation=2, incarnation=INC, pinned_requirements=dict(PINS))
        msg = self.b.post("J", "lead", f"GOAL-READY {SHA1} req={self.requirements_digest(PINS)}")
        self.receipt("goal_ready", msg.id, 1, {"kind": "goal_ready", "id": str(msg.id), "rev": 1,
                                               "generation": 2, "candidate": SHA1}, generation=2)
        self.assertEqual(self.read(led2).goal_ready["id"], msg.id)
        self.assertIsNone(self.read(self.led).goal_ready)          # generation 1 does not see it

    def test_goal_ready_takes_the_newest_bound_one(self):
        self.goal_ready(SHA1)
        newer = self.goal_ready(SHA2)
        gr = self.read().goal_ready
        self.assertEqual((gr["id"], gr["candidate"]), (newer, SHA2))

    # ---- the ledger's own state
    def test_live_launches_are_those_with_a_claiming_or_claimed_intent(self):
        self.led.intents = [
            Intent("L1", "gen1|launch|a|-|1|-", 1, "a", 1.0, 1.0, [], "claimed"),
            Intent("L2", "gen1|launch|b|-|1|-", 1, "b", 1.0, 1.0, [], "claiming"),
            Intent("L3", "gen1|launch|c|-|1|-", 1, "c", 1.0, 1.0, [], "concluded", "completed", True, 0.5),
        ]
        self.assertEqual(self.read().live, {"a", "b"})

    def test_halted_follows_the_ledger_and_status_the_board(self):
        self.assertFalse(self.read().halted)
        self.assertEqual(self.read().status, "active")
        self.led.halted = {"at": NOW.isoformat(), "by": "person"}
        self.assertTrue(self.read().halted)

    # ---- drive_cfg: the recipe comes from the owner record, and fails closed
    @staticmethod
    def owner(details):
        return mock.patch("swarm.enrolment.find_job_owner", return_value=SimpleNamespace(owner=details))

    @staticmethod
    def full_owner(recipe, review_role, review_gates=None):
        return {"recipe": recipe, "review_role": review_role, "review_gates": review_gates}

    def test_drive_cfg_reads_the_recipe_from_the_owner_record(self):
        with self.owner(self.full_owner("artifact", None, None)):
            cfg = ds.drive_cfg(BK, "J", {"recipe": "coding", "max_rounds": 5})
        self.assertEqual((cfg["recipe"], cfg["review_role"], cfg["review_gates"]), ("artifact", None, None))
        self.assertEqual(cfg["max_rounds"], 5)

    def test_drive_cfg_coding_recipe_keeps_its_reviewer_and_gates(self):
        with self.owner(self.full_owner("coding", "reviewer", "haiku-strict")):
            cfg = ds.drive_cfg(BK, "J", {})
        self.assertEqual((cfg["recipe"], cfg["review_role"], cfg["review_gates"]),
                         ("coding", "reviewer", "haiku-strict"))

    def test_drive_cfg_never_takes_the_recipe_from_config(self):
        with self.owner(self.full_owner("artifact", None, None)):
            self.assertEqual(ds.drive_cfg(BK, "J", {"recipe": "coding"})["recipe"], "artifact")

    def test_drive_cfg_fails_closed_without_an_owner_record(self):
        with mock.patch("swarm.enrolment.find_job_owner", return_value=None):
            with self.assertRaises(ds.DriveConfigError):
                ds.drive_cfg(BK, "J", {"recipe": "coding"})

    def test_drive_cfg_fails_closed_when_any_recipe_key_is_missing(self):
        for dropped in ("recipe", "review_role", "review_gates"):
            details = self.full_owner("coding", "reviewer", "standard")
            del details[dropped]
            with self.subTest(dropped=dropped), self.owner(details):
                with self.assertRaises(ds.DriveConfigError):
                    ds.drive_cfg(BK, "J", {})

    def test_drive_cfg_fails_closed_on_an_unknown_recipe(self):
        with self.owner(self.full_owner("haiku-only", "reviewer", "standard")):
            with self.assertRaises(ds.DriveConfigError):
                ds.drive_cfg(BK, "J", {})


class WakeMapping(unittest.TestCase):
    """_wakes on a stand-in board: the launch-to-agent mapping (departed members too), the two watermarks, the
    receipt gate on posts, and events matched by name case-insensitively."""

    class Board:
        """`agents` maps agent key to name; `departed` is the set of keys whose agent has left."""
        def __init__(self, agents, departed=()):
            self._agents, self._departed = agents, set(departed)

        def agents(self, job, include_departed=True):
            return [SimpleNamespace(agent_key=k, name=n, ended_at=("gone" if k in self._departed else None))
                    for k, n in self._agents.items() if include_departed or k not in self._departed]

    class Rc:
        def __init__(self, ok_ids):
            self.ok_ids = set(ok_ids)

        def get(self, kind, record_id, rev, row):
            return object() if record_id in self.ok_ids else None

    @staticmethod
    def launches(**by_launch):
        return {s: SimpleNamespace(agent_key=k) for s, k in by_launch.items()}

    @staticmethod
    def msg(id, to, text="x"):
        return SimpleNamespace(id=id, to_agent=to, message=text)

    @staticmethod
    def ev(id, to):
        return SimpleNamespace(id=id, to=to)

    def wakes(self, *, agents, launches, msgs=(), events=(), watermarks=None, ok=None, departed=()):
        led = SimpleNamespace(watermarks=watermarks or {})
        ok = [m.id for m in msgs] if ok is None else ok
        return ds._wakes(self.Board(agents, departed), "J", launches, list(msgs), list(events), led, self.Rc(ok))

    def test_a_departed_members_post_wakes_its_resumable_launch(self):
        got = self.wakes(agents={"k1": "Gone Agent"}, launches=self.launches(a="k1"),
                         msgs=[self.msg(5, "Gone Agent")], departed=["k1"])
        self.assertEqual(got, {"a": [5]})

    def test_an_unreceipted_post_does_not_wake(self):
        got = self.wakes(agents={"k1": "A"}, launches=self.launches(a="k1"), msgs=[self.msg(5, "A")], ok=[])
        self.assertEqual(got, {})

    def test_a_post_at_the_message_watermark_is_not_a_wake(self):
        got = self.wakes(agents={"k1": "A"}, launches=self.launches(a="k1"),
                         msgs=[self.msg(5, "A"), self.msg(6, "A")], watermarks={"msg:a": 5})
        self.assertEqual(got, {"a": [6]})

    def test_the_message_watermark_does_not_hide_events(self):
        got = self.wakes(agents={"k1": "A"}, launches=self.launches(a="k1"), msgs=[self.msg(900, "A")],
                         events=[self.ev(3, "a")], watermarks={"msg:a": 900})
        self.assertEqual(got, {"a": [3]})

    def test_the_event_watermark_does_not_hide_posts(self):
        got = self.wakes(agents={"k1": "A"}, launches=self.launches(a="k1"), msgs=[self.msg(4, "A")],
                         events=[self.ev(50, "a")], watermarks={"evack:a:50": 1})
        self.assertEqual(got, {"a": [4]})

    def test_an_acknowledged_event_is_not_a_wake_and_hides_no_other(self):
        got = self.wakes(agents={"k1": "A"}, launches=self.launches(a="k1"),
                         events=[self.ev(50, "a"), self.ev(51, "a")], watermarks={"evack:a:50": 1})
        self.assertEqual(got, {"a": [51]})

    def test_an_acknowledged_far_event_never_wakes_again_and_a_lower_one_still_does(self):
        got = self.wakes(agents={"k1": "A"}, launches=self.launches(a="k1"),
                         events=[self.ev(10 ** 9, "a"), self.ev(60, "a")], watermarks={f"evack:a:{10 ** 9}": 1})
        self.assertEqual(got, {"a": [60]})
        self.assertEqual(self.wakes(agents={"k1": "A"}, launches=self.launches(a="k1"), events=[self.ev(10 ** 9, "a")],
                                    watermarks={f"evack:a:{10 ** 9}": 1}), {})

    def test_an_event_ack_of_another_launch_does_not_count(self):
        got = self.wakes(agents={"k1": "A"}, launches=self.launches(a="k1"), events=[self.ev(50, "a")],
                         watermarks={"evack:b:50": 1, "event:a": 99})
        self.assertEqual(got, {"a": [50]})

    def test_event_names_match_case_insensitively(self):
        got = self.wakes(agents={"k1": "Mixed Case"}, launches=self.launches(a="k1"),
                         events=[self.ev(7, "mixed case")])
        self.assertEqual(got, {"a": [7]})

    def test_a_launch_without_an_agent_key_gets_no_wakes(self):
        got = self.wakes(agents={"k1": "A"}, launches={"a": SimpleNamespace(agent_key=None)},
                         msgs=[self.msg(5, "A"), self.msg(6, None)])     # a broadcast is no one's wake
        self.assertEqual(got, {})


class GoalNeedsLandedWorkOnTheBoard:
    """E-land on int/rc 32d21ea6: an engineer's DONE never receipted leaves no hand-off group, and the goal step
    read "no groups" as "all integrated" (a coding job reached goal MET with nothing landed). Mixed into
    SnapshotCase: every backend."""

    def goal_prompts(self, led=None, recipe="coding"):
        from swarm.supervisor.driver_model import next_actions
        from test_drive_next_actions import ARTIFEXT_CFG, CFG
        acts = next_actions(self.read(led), CFG if recipe == "coding" else ARTIFEXT_CFG)
        return [x.prompt_kind for x in acts]

    def test_unreceipted_done_is_no_group_and_no_goal_judge_for_a_coding_job(self):
        self.done("feat/a@" + SHA1, receipted=False)
        self.goal_ready()
        self.assertEqual(self.read().handoffs, [])
        self.assertNotIn("goal", self.goal_prompts())

    def test_artifact_job_with_no_groups_is_unaffected(self):
        self.goal_ready()
        self.assertIn("goal", self.goal_prompts(recipe="artifact"))

    def test_goal_judge_launches_once_every_group_is_integrated(self):
        art = "feat/a@" + SHA1
        self.done(art)
        self.artifact_verdict(art, verdict="met", open_count=0)
        self.goal_ready()
        self.assertNotIn("goal", self.goal_prompts(), "a receipted but unlanded group is not ready")
        dest = {"https://example.test/r.git": {"state": "verified", "sha": SHA1}}
        self.led.integrated = {"feat/a": {"landed": SHA1, "land_id": 7, "land_rev": 1, "destinations": dest}}
        self.receipt("land", 7, 1, {"destinations": dest}, launch=None)
        self.assertIn("goal", self.goal_prompts())


class MemorySnapshot(GoalNeedsLandedWorkOnTheBoard, SnapshotCase, unittest.TestCase):
    harness_factory = staticmethod(drive_backends()["memory"])


add_backend_variants(globals(), MemorySnapshot)   # FileSnapshot, SqliteSnapshot, PostgresSnapshot (SWARM_TEST_CONFIG)
