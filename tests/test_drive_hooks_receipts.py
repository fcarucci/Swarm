"""Receipts of the drive hooks (spec 4.0, 4.5; Task 5 and 5b): what the PostToolUse hook receipts, from whom,
bound to what. Each kind has a positive test and negatives for a forged row, a swapped record, a caller without
the authority and a record that disagrees with the submitted arguments (nothing is written, and the
disagreement is logged). Payloads have the shapes the hosts send."""
from __future__ import annotations

import json
import os
import time
import types
from pathlib import Path
from unittest import mock

from drive_hooks_support import DriveCase, ISO, REQ
from support import posix_only

from swarm import drivegate, enrolment, hosts, paths, receipts
from swarm.supervisor import drive_authz, ledger

ART = "feat/a@" + "a" * 40
CAND = "main@" + "b" * 40


def rec(kind, rid, rev=1):
    return f"swarm-record: kind={kind} id={rid} rev={rev}"


class RecordFormat(DriveCase):
    def test_every_record_kind_maps_to_a_receipt_kind(self):
        for kind in ("done", "review", "verdict", "ask", "answer", "post", "goal_ready", "ack",
                     "withdraw", "halt", "unhalt", "deactivate", "activate"):
            with self.subTest(kind=kind):
                self.assertIn(drive_authz.receipt_kind(kind), receipts.KINDS)
        self.assertIsNone(drive_authz.receipt_kind("run"))
        self.assertEqual(drive_authz.receipt_kind("halt"), "person")
        self.assertIsNone(drive_authz.receipt_kind("nonsense"))

    def test_claude_payload_has_no_exit_code_and_still_yields_the_record(self):
        """H2: the captured Claude Code 2.1.284 PostToolUse(Bash) payload has no exit_code; success is the
        CLI's printed record line."""
        fixture = json.loads((Path(__file__).parent / "fixtures/claude/2.1.284/hooks/PostToolUse-Bash-record.json")
                             .read_text())
        self.assertNotIn("exit_code", fixture["tool_response"])
        self.assertEqual(drivegate.record_of(fixture), ("done", 42, 1))

    def test_codex_payload_is_a_plain_string(self):
        fixture = json.loads((Path(__file__).parent / "fixtures/codex/0.157.1/hooks/PostToolUse-Bash-record.json")
                             .read_text())
        self.assertIsInstance(fixture["tool_response"], str)
        self.assertEqual(drivegate.record_of(fixture), ("done", 42, 1))

    def test_record_line_must_be_the_last_exact_line(self):
        ok = rec("done", 7)
        for text, want in ((ok + "\n", ("done", 7, 1)), ("hello\n" + ok, ("done", 7, 1)), (ok + "\nmore\n", None),
                           (ok + " ", ("done", 7, 1)), ("x " + ok, None), ("swarm-record: kind=Done id=7 rev=1", None),
                           ("swarm-record: kind=done id=7", None), ("", None)):
            with self.subTest(text=text):
                self.assertEqual(drivegate.record_of({"tool_response": {"stdout": text}}), want)
                self.assertEqual(drivegate.record_of({"tool_response": text}), want)

    def test_an_interrupted_or_failed_call_has_no_record(self):
        out = rec("done", 7)
        self.assertIsNone(drivegate.record_of({"tool_response": {"stdout": out, "interrupted": True}}))
        self.assertIsNone(drivegate.record_of({"tool_response": {"stdout": out, "exit_code": 1}}))
        self.assertEqual(drivegate.record_of({"tool_response": {"stdout": out, "exit_code": 0}}), ("done", 7, 1))
        self.assertIsNone(drivegate.record_of({"tool_response": None}))
        self.assertIsNone(drivegate.record_of({}))

    def test_intermixed_positionals_parse_on_every_python(self):
        ns = drivegate.parse_swarm(["answer", "3", "--key", "k", "sqlite", "now"])
        # The CLI's own parser (with its plugins) may type the id; the grant fingerprint stringifies every value.
        self.assertEqual((ns.cmd, str(ns.id), ns.key, ns.text), ("answer", "3", "k", ["sqlite", "now"]))
        ns = drivegate.parse_swarm(["ask", "--job", "J", "--to", "human", "Which", "DB?", "--default", "x"])
        self.assertEqual((ns.text, ns.default), (["Which", "DB?"], "x"))

    def test_submitted_argv_includes_defaults_in_a_fixed_order(self):
        ns = drivegate.parse_swarm(["run", "--name", "s", "--job", "J", "--role", "engineer",
                                      "--brief", "B.md"])
        self.assertEqual(drivegate.submitted_argv(ns), ["run", "--brief", "B.md", "--harness", "claude",
                                                          "--job", "J", "--name", "s", "--role", "engineer"])
        other = drivegate.parse_swarm(["run", "--name", "s", "--job", "J", "--role", "engineer",
                                         "--brief", "B.md", "--harness", "codex"])
        self.assertNotEqual(drivegate.submitted_argv(ns), drivegate.submitted_argv(other))



class LandReceipts(DriveCase):
    """QA live-met: an integrator seat's `swarm land` printed `kind=post` and got no receipt, so the driver never
    saw a receipted LAND-REQUEST and nothing landed. Its LAND-REQUEST post is receipted like a post, from the
    integrator seat only."""
    SEAT = "integrator-feat-a-00000000"
    CMD = f"swarm land --job J --artifact {ART}"

    def test_land_from_the_integrator_seat_writes_a_post_receipt_the_driver_finds(self):
        from swarm import land
        sid, _ = self.launch_seat(self.SEAT, role="integrator", kind="integrate")
        rid = self.post_as(sid, land.request_text(ART))
        self.assertTrue(self.post(sid, self.CMD, rec("post", rid)))
        r = self.receipt("post", rid)
        self.assertEqual((r.seat, r.kind), (self.SEAT, "post"))
        self.assertTrue(receipts.matches(r, {"kind": "post", "id": str(rid), "rev": 1, "to": None,
                                             "body": land.request_text(ART)}))

    def test_land_naming_another_artifact_or_from_a_non_integrator_gets_no_receipt(self):
        from swarm import land
        sid, _ = self.launch_seat(self.SEAT, role="integrator", kind="integrate")
        rid = self.post_as(sid, land.request_text("feat/a@" + "c" * 40))
        self.assertFalse(self.post(sid, self.CMD, rec("post", rid)))
        eng, _ = self.launch_seat("eng-a")
        rid = self.post_as(eng, land.request_text(ART))
        self.assertFalse(self.post(eng, self.CMD, rec("post", rid)))
        self.assertIsNone(self.receipt("post", rid))

    def test_land_hosts_records_nothing(self):
        sid, _ = self.launch_seat(self.SEAT, role="integrator", kind="integrate")
        self.assertFalse(self.post(sid, "swarm land hosts add git@h:o/r.git", rec("post", 1)))

class DoneReceipts(DriveCase):
    CMD = f"swarm done --job J --artifact {ART} --summary x"

    def test_done_from_seat_session_writes_receipt(self):
        sid, _ = self.launch_seat("eng-a")
        rid = self.post_as(sid, f'DONE "{ART}" | x')
        self.assertTrue(self.post(sid, self.CMD, rec("done", rid)))
        r = self.receipt("done", rid)
        self.assertEqual((r.seat, r.action_id, r.attempt, r.generation), ("eng-a", self.action, 1, 1))
        self.assertTrue(receipts.matches(r, {"kind": "done", "id": str(rid), "rev": 1, "artifact": ART,
                                             "sha": "a" * 40, "group": "feat/a"}))
        ns = drivegate.parse_swarm(self.CMD.split()[1:])
        self.assertEqual(r.args_sha, receipts.args_sha(drivegate.submitted_argv(ns)))
        self.kick.assert_called_once()

    def test_done_without_job_gets_the_seats_job_inserted_and_is_receipted(self):
        # QA live-met fdd441e: `swarm done --artifact ...` with no --job was refused and the seat could not hand off
        from swarm import hooks
        sid, _ = self.launch_seat("eng-a")
        rid = self.post_as(sid, f'DONE "{ART}" | x')
        bare = f"swarm done --artifact {ART} --summary x"
        self.assertFalse(self.post(sid, bare, rec("done", rid)), "a done that names no job is not receipted")
        rewritten = hooks.own_job_command(bare, "J")
        self.assertEqual(rewritten, self.CMD)
        self.assertTrue(self.post(sid, rewritten, rec("done", rid)))
        self.assertIsNotNone(self.receipt("done", rid))

    def test_done_naming_another_job_is_left_alone_and_a_trailing_echo_is_not_receipted(self):
        from swarm import hooks
        self.assertIsNone(hooks.own_job_command(f"swarm done --job K --artifact {ART}", "J"))
        self.assertEqual(hooks.own_job_command("swarm run --name a --role engineer --brief B.md", "J"), "swarm run --job J --name a --role engineer --brief B.md")
        sid, _ = self.launch_seat("eng-a")
        rid = self.post_as(sid, f'DONE "{ART}" | x')
        # the record line must be the output's last line: a trailing `; echo` loses it (the prompt forbids it)
        out = f"posted #1\n{rec('done', rid)}\nok\n"
        p = self.payload(sid, "Bash", self.CMD + "; echo ok", stdout=out)
        self.assertFalse(drivegate.post_tool(self.cfg, p, self.host))

    def test_done_by_the_real_cli_through_a_seat_key(self):
        sid, _ = self.launch_seat("eng-a")
        rc, out, err = self.cli("done", "--job", "J", "--key", sid, "--artifact", ART, "--summary", "x")
        self.assertEqual(rc, 0, err)
        rid = int(out.split("#")[1].split()[0])
        self.assertTrue(self.post(sid, f"swarm done --job J --key {sid} --artifact {ART} --summary x", rec("done", rid)))
        self.assertIsNotNone(self.receipt("done", rid))

    def test_done_whose_message_says_another_artifact_gets_no_receipt(self):
        sid, _ = self.launch_seat("eng-a")
        rid = self.post_as(sid, 'DONE "feat/other@' + "c" * 40 + '" | x')
        self.assertFalse(self.post(sid, self.CMD, rec("done", rid)))
        self.assertIsNone(self.receipt("done", rid))
        self.assertIn("not what the command posted", self.failures())

    def test_done_naming_another_agents_message_gets_no_receipt(self):
        sid, _ = self.launch_seat("eng-a")
        other, _ = self.launch_seat("eng-b")
        rid = self.post_as(other, f'DONE "{ART}" | x')
        self.assertFalse(self.post(sid, self.CMD, rec("done", rid)))
        self.assertIsNone(self.receipt("done", rid))

    def test_forged_echo_of_a_record_line_cannot_name_a_foreign_message(self):
        sid, _ = self.launch_seat("eng-a")
        other, _ = self.launch_seat("eng-b")
        rid = self.post_as(other, f'DONE "{ART}" | x')
        self.assertFalse(self.post(sid, self.CMD + f"; echo '{rec('done', rid)}'", rec("done", rid)))
        self.assertIsNone(self.receipt("done", rid))


    def test_done_by_a_subagent_of_the_seat_gets_no_receipt(self):
        """The payload's own agent_id marks a subagent: it carries its parent's session id and no authority."""
        sid, _ = self.launch_seat("eng-a")
        rid = self.post_as(sid, f'DONE "{ART}" | x')
        self.assertFalse(self.post(sid, self.CMD, rec("done", rid), agent_id="sub-1"))
        self.assertIsNone(self.receipt("done", rid))

    def test_done_by_a_seat_that_has_not_joined_the_board_gets_no_receipt(self):
        sid, _ = self.launch_seat("eng-a", bind=False)
        rid = self.post_as_name("someone", f'DONE "{ART}" | x')
        self.assertFalse(self.post(sid, self.CMD, rec("done", rid)))

    def test_done_with_two_record_commands_gets_no_receipt(self):
        sid, _ = self.launch_seat("eng-a")
        rid = self.post_as(sid, f'DONE "{ART}" | x')
        self.assertFalse(self.post(sid, self.CMD + " && " + self.CMD, rec("done", rid)))

    def test_done_after_the_launch_concluded_gets_no_receipt(self):
        sid, _ = self.launch_seat("eng-a", state="concluded")
        rid = self.post_as(sid, f'DONE "{ART}" | x')
        self.assertFalse(self.post(sid, self.CMD, rec("done", rid)))

    def test_done_by_a_seat_whose_launch_is_not_in_the_ledger_gets_no_receipt(self):
        sid, _ = self.launch_seat("eng-a")
        self.edit(lambda l: setattr(l, "intents", []))
        rid = self.post_as(sid, f'DONE "{ART}" | x')
        self.assertFalse(self.post(sid, self.CMD, rec("done", rid)))

    def test_done_without_an_artifact_gets_no_receipt(self):
        sid, _ = self.launch_seat("eng-a")
        rid = self.post_as(sid, 'DONE "handoff-1" | x')
        self.assertFalse(self.post(sid, "swarm done --job J --summary x", rec("done", rid)))
        self.assertIn("names its artifact", self.failures())

    def test_branch_and_sha_spell_the_artifact(self):
        sid, _ = self.launch_seat("eng-a")
        rid = self.post_as(sid, f'DONE "{ART}" | x')
        self.assertTrue(self.post(sid, f"swarm done --job J --branch feat/a --sha {'a' * 40} --summary x",
                                  rec("done", rid)))

    def test_a_record_of_another_kind_than_the_command_gets_no_receipt(self):
        sid, _ = self.launch_seat("eng-a")
        rid = self.post_as(sid, f'DONE "{ART}" | x')
        self.assertFalse(self.post(sid, self.CMD, rec("post", rid)))

    def test_the_job_comes_from_the_launch_not_the_flag(self):
        sid, _ = self.launch_seat("eng-a")
        rid = self.post_as(sid, f'DONE "{ART}" | x')
        self.assertFalse(self.post(sid, self.CMD.replace("--job J", "--job OTHER"), rec("done", rid)))

    def test_a_failed_receipt_write_is_logged(self):
        sid, _ = self.launch_seat("eng-a")
        rid = self.post_as(sid, f'DONE "{ART}" | x')
        with mock.patch.object(receipts, "write", side_effect=OSError("disk gone")):
            self.assertFalse(self.post(sid, self.CMD, rec("done", rid)))
        self.assertIn("disk gone", self.failures())
        self.kick.assert_not_called()

    def test_hook_after_cli_before_receipt_barrier(self):
        """A crash between the CLI's write and the receipt's leaves no receipt (the driver ignores the record);
        running the hook again writes it."""
        sid, _ = self.launch_seat("eng-a")
        rid = self.post_as(sid, f'DONE "{ART}" | x')
        with mock.patch.dict(os.environ, {"SWARM_CRASH_AT": "hook_after_cli_before_receipt"}), \
                mock.patch("os.kill", side_effect=SystemExit("killed")):
            with self.assertRaises(SystemExit):
                self.post(sid, self.CMD, rec("done", rid))
        self.assertIsNone(self.receipt("done", rid))
        self.assertTrue(self.post(sid, self.CMD, rec("done", rid)))
        self.assertIsNotNone(self.receipt("done", rid))


class ReviewReceipts(DriveCase):
    def test_review_receipt_binds_group_artifact_and_outcome(self):
        sid, _ = self.launch_seat("reviewer-a-1", role="reviewer", kind="review")
        rid = self.post_as(sid, f"REVIEW {ART} VERIFIED r.md")
        cmd = f"swarm review --job J --artifact {ART} verified --report r.md"
        self.assertTrue(self.post(sid, cmd, rec("review", rid)))
        r = self.receipt("review", rid)
        self.assertTrue(receipts.matches(r, {"kind": "review", "id": str(rid), "rev": 1, "group": "feat/a",
                                             "artifact": ART, "outcome": "verified"}))
        self.assertFalse(receipts.matches(r, {"kind": "review", "id": str(rid), "rev": 1, "group": "feat/a",
                                              "artifact": ART, "outcome": "changes"}))

    def test_review_with_an_inline_report_and_capital_outcome_is_receipted(self):
        from swarm.drive_cli import INLINE_REPORT
        sid, _ = self.launch_seat("reviewer-a-1", role="reviewer", kind="review")
        rid = self.post_as(sid, f"REVIEW {ART} CHANGES {INLINE_REPORT}")
        cmd = f'swarm review --job J --artifact {ART} CHANGES --report-text "the test passes on the base"'
        self.assertTrue(self.post(sid, cmd, rec("review", rid)))
        self.assertTrue(receipts.matches(self.receipt("review", rid), {
            "kind": "review", "id": str(rid), "rev": 1, "group": "feat/a", "artifact": ART, "outcome": "changes"}))

    def test_review_whose_message_says_changes_gets_no_receipt(self):
        sid, _ = self.launch_seat("reviewer-a-1", role="reviewer", kind="review")
        rid = self.post_as(sid, f"REVIEW {ART} CHANGES r.md")
        self.assertFalse(self.post(sid, f"swarm review --job J --artifact {ART} verified --report r.md",
                                   rec("review", rid)))

    def test_judge_cannot_supply_its_own_independent_review(self):
        for name, role, kind in (("judge", "judge", "judge"),):
            sid, _ = self.launch_seat(name, role=role, kind=kind)
            rid = self.post_as(sid, f"REVIEW {ART} VERIFIED r.md")
            with self.subTest(role=role):
                self.assertFalse(self.post(sid, f"swarm review --job J --artifact {ART} verified --report r.md",
                                           rec("review", rid)))


class VerdictReceipts(DriveCase):
    def judge(self):
        sid, _ = self.launch_seat("judge", role="judge", kind="judge")
        return sid

    def verdict(self, sid, verdict, reason, cmd_tail, scope="artifact", candidate=None, req=None, **kw):
        from swarm.review import verdict_text   # the CLI's one VERDICT format (6b: a goal verdict names what it judges)
        rid = self.post_as(sid, verdict_text(verdict, reason, scope=scope, generation=1, candidate=candidate, req=req))
        return rid, self.post(sid, f"swarm verdict --job J {cmd_tail}", rec("verdict", rid), **kw)

    def decision(self, rid, **over):
        d = {"kind": "verdict", "id": str(rid), "rev": 1, "verdict": "met", "scope": "artifact", "generation": 1,
             "candidate": None, "req": None, "artifact": ART}
        d.update(over)
        return d

    def test_artifact_verdict_receipt_binds_the_artifact(self):
        sid = self.judge()
        rid, ok = self.verdict(sid, "met", "looks right", f"--artifact {ART} met 'looks right'")
        self.assertTrue(ok)
        r = self.receipt("verdict", rid)
        self.assertTrue(receipts.matches(r, self.decision(rid)))
        self.assertFalse(receipts.matches(r, self.decision(rid, artifact="feat/b@" + "b" * 40)))
        self.assertFalse(receipts.matches(r, self.decision(rid, verdict="not_met")))

    def test_goal_verdict_receipt_binds_the_candidate_and_req(self):
        sid = self.judge()
        rid, ok = self.verdict(sid, "met", "goal met",
                               f"--scope goal --candidate {CAND} --req R1 met 'goal met'",
                               scope="goal", candidate=CAND, req="R1")
        self.assertTrue(ok)
        r = self.receipt("verdict", rid)
        self.assertTrue(receipts.matches(r, self.decision(rid, scope="goal", candidate=CAND, req="R1",
                                                          artifact=None)))
        self.assertFalse(receipts.matches(r, self.decision(rid, scope="goal", candidate="main@" + "c" * 40,
                                                           req="R1", artifact=None)))

    def test_unreachable_defaults_to_the_goal_scope(self):
        sid = self.judge()
        rid, ok = self.verdict(sid, "unreachable", "no way",
                               f"--candidate {CAND} --reason 'no way' --details - unreachable",
                               scope="goal", candidate=CAND)
        self.assertTrue(ok)
        self.assertEqual(self.receipt("verdict", rid).kind, "verdict")

    def test_an_artifact_verdict_without_its_artifact_gets_no_receipt(self):
        sid = self.judge()
        rid, ok = self.verdict(sid, "met", "fine", "met fine")
        self.assertFalse(ok)
        self.assertIn("names its artifact", self.failures())

    def test_a_goal_verdict_without_its_candidate_gets_no_receipt(self):
        sid = self.judge()
        rid, ok = self.verdict(sid, "met", "goal met", "--scope goal --req R1 met 'goal met'")
        self.assertFalse(ok)
        self.assertIn("names its candidate", self.failures())

    def test_the_verdict_receipt_carries_the_ledgers_generation(self):
        sid = self.judge()
        self.edit(lambda l: setattr(l, "generation", 2))
        with self.board() as b:
            b.queue_launch("J", 2, "judge", role="judge", harness="claude", model="opus",
                           workdir=str(self.work), brief="B.md", queued_by="driver")
        rid = self.post_as(sid, "VERDICT met: fine")
        self.assertTrue(self.post(sid, f"swarm verdict --job J --artifact {ART} met fine", rec("verdict", rid)))
        r = self.receipt("verdict", rid, generation=2)
        self.assertIsNotNone(r)
        self.assertTrue(receipts.matches(r, self.decision(rid, generation=2)))
        self.assertFalse(receipts.matches(r, self.decision(rid, generation=1)))

    def test_verdict_message_that_says_another_verdict_gets_no_receipt(self):
        sid = self.judge()
        rid = self.post_as(sid, "VERDICT not_met: nope")
        self.assertFalse(self.post(sid, f"swarm verdict --job J --artifact {ART} met 'nope'", rec("verdict", rid)))

    def test_verdict_from_a_non_judge_gets_no_receipt(self):
        for name, role in (("eng-a", "engineer"), ("lead", "engineering_lead")):
            sid, _ = self.launch_seat(name, role=role)
            rid = self.post_as(sid, "VERDICT met: fine")
            with self.subTest(role=role):
                self.assertFalse(self.post(sid, f"swarm verdict --job J --artifact {ART} met fine", rec("verdict", rid)))

    def test_verdict_show_is_not_a_record(self):
        sid = self.judge()
        self.assertFalse(self.post(sid, "swarm verdict --job J show", rec("verdict", 3)))

    def test_every_decision_field_changes_the_receipt_digest(self):
        base = self.decision(1)
        shas = {receipts.content_sha("verdict", base)}
        for field, value in (("verdict", "not_met"), ("scope", "goal"), ("generation", 2), ("candidate", CAND),
                             ("req", "R"), ("artifact", "x@" + "d" * 40)):
            shas.add(receipts.content_sha("verdict", dict(base, **{field: value})))
        self.assertEqual(len(shas), 7)


class GoalReadyReceipts(DriveCase):
    def setUp(self):
        super().setUp()
        self.edit(lambda l: l.pinned_requirements.update({"/w/REQ.md": "e" * 40}))
        self.digest = drive_authz.requirements_digest({"/w/REQ.md": "e" * 40})

    def test_lead_goal_ready_is_receipted_with_the_ledger_digest(self):
        sid, _ = self.launch_seat("lead", role="engineering_lead", kind="lead")
        rid = self.post_as(sid, f"GOAL-READY {CAND} req={self.digest}")
        self.assertTrue(self.post(sid, f"swarm goal-ready --job J --candidate {CAND}", rec("goal_ready", rid)))
        r = self.receipt("goal_ready", rid)
        self.assertTrue(receipts.matches(r, {"kind": "goal_ready", "id": str(rid), "rev": 1, "generation": 1,
                                             "candidate": CAND}))

    def test_requester_goal_ready_is_receipted(self):
        with self.board() as b:
            rid = b.post("J", "person", f"GOAL-READY {CAND} req={self.digest}").id
        self.assertTrue(self.post(REQ, f"swarm goal-ready --job J --candidate {CAND}", rec("goal_ready", rid)))

    def test_a_goal_ready_over_no_pinned_requirements_is_invalid(self):
        self.edit(lambda l: l.pinned_requirements.clear())
        sid, _ = self.launch_seat("lead", role="engineering_lead", kind="lead")
        rid = self.post_as(sid, f"GOAL-READY {CAND} req={drive_authz.requirements_digest({})}")
        self.assertFalse(self.post(sid, f"swarm goal-ready --job J --candidate {CAND}", rec("goal_ready", rid)))
        self.assertIn("pins no requirements", self.failures())

    def test_a_req_that_is_not_the_ledgers_digest_gets_no_receipt(self):
        sid, _ = self.launch_seat("lead", role="engineering_lead", kind="lead")
        rid = self.post_as(sid, f"GOAL-READY {CAND} req={'f' * 64}")
        self.assertFalse(self.post(sid, f"swarm goal-ready --job J --candidate {CAND}", rec("goal_ready", rid)))

    def test_goal_ready_by_an_engineer_gets_no_receipt(self):
        sid, _ = self.launch_seat("eng-a")
        rid = self.post_as(sid, f"GOAL-READY {CAND} req={self.digest}")
        self.assertFalse(self.post(sid, f"swarm goal-ready --job J --candidate {CAND}", rec("goal_ready", rid)))

    def test_goal_ready_with_no_pinned_requirements_gets_no_receipt(self):
        self.edit(lambda l: l.pinned_requirements.clear())
        sid, _ = self.launch_seat("lead", role="engineering_lead", kind="lead")
        rid = self.post_as(sid, f"GOAL-READY {CAND} req={self.digest}")
        self.assertFalse(self.post(sid, f"swarm goal-ready --job J --candidate {CAND}", rec("goal_ready", rid)))

    def test_requirements_digest_is_order_independent_and_content_bound(self):
        a = {"/p/a.md": "1" * 40, "/p/b.md": "2" * 40}
        self.assertEqual(drive_authz.requirements_digest(a), drive_authz.requirements_digest(dict(reversed(a.items()))))
        self.assertNotEqual(drive_authz.requirements_digest(a),
                            drive_authz.requirements_digest({**a, "/p/b.md": "3" * 40}))


class PostAskAnswerReceipts(DriveCase):
    def test_addressed_post_is_receipted_with_its_row(self):
        sid, _ = self.launch_seat("eng-a")
        lead, _ = self.launch_seat("lead", role="engineering_lead", kind="lead")
        to = self.name_of(lead)
        rid = self.post_as(sid, "please look", to=to)
        self.assertTrue(self.post(sid, f"swarm post --job J --key {sid} --to '{to}' please look", rec("post", rid)))
        r = self.receipt("post", rid)
        self.assertTrue(receipts.matches(r, {"kind": "post", "id": str(rid), "rev": 1, "to": to, "body": "please look"}))

    def test_post_to_a_role_needs_an_addressed_message(self):
        sid, _ = self.launch_seat("eng-a")
        lead, _ = self.launch_seat("lead", role="engineering_lead", kind="lead")
        rid = self.post_as(sid, "ping", to=self.name_of(lead))
        self.assertTrue(self.post(sid, f"swarm post --job J --key {sid} --to @EL ping", rec("post", rid)))
        rid2 = self.post_as(sid, "ping")     # a broadcast under a command that said it was addressed
        self.assertFalse(self.post(sid, f"swarm post --job J --key {sid} --to @EL ping", rec("post", rid2)))

    def test_unaddressed_post_gets_no_receipt_and_no_log(self):
        sid, _ = self.launch_seat("eng-a")
        rid = self.post_as(sid, "hello")
        self.assertFalse(self.post(sid, f"swarm post --job J --key {sid} hello", rec("post", rid)))
        self.assertEqual(self.failures(), "")

    def test_post_whose_text_or_target_differs_gets_no_receipt(self):
        sid, _ = self.launch_seat("eng-a")
        lead, _ = self.launch_seat("lead", role="engineering_lead", kind="lead")
        to = self.name_of(lead)
        rid = self.post_as(sid, "something else", to=to)
        self.assertFalse(self.post(sid, f"swarm post --job J --key {sid} --to '{to}' please look", rec("post", rid)))
        rid = self.post_as(sid, "please look", to="someone else")
        self.assertFalse(self.post(sid, f"swarm post --job J --key {sid} --to '{to}' please look", rec("post", rid)))

    def test_the_requester_does_not_ask(self):
        sid, _ = self.launch_seat("eng-a")
        b = self.open_question(sid)
        self.assertFalse(self.post(REQ, "swarm ask --job J --to human --default sqlite Which DB?", rec("ask", b.id)))
        self.assertEqual(self.failures(), "")

    def test_requester_post_is_receipted(self):
        lead, _ = self.launch_seat("lead", role="engineering_lead", kind="lead")
        to = self.name_of(lead)
        with self.board() as b:
            rid = b.post("J", "person", "go", to).id
        self.assertTrue(self.post(REQ, f"swarm post --job J --as person --to '{to}' go", rec("post", rid)))
        self.assertEqual(self.receipt("post", rid).action_id, "requester:post")

    def open_question(self, sid, text="Which DB?", default="sqlite", to="human"):
        with self.board() as b:
            until = b.now() + __import__("datetime").timedelta(hours=4)
            return b.open_blocker("J", "question", to, text, until=until, default_value=default,
                                  created_by=b.active_agent_name(sid), seat="eng-a", generation=1)

    def test_ask_is_receipted_with_question_until_and_default(self):
        sid, _ = self.launch_seat("eng-a")
        b = self.open_question(sid)
        cmd = f"swarm ask --job J --key {sid} --to human --default sqlite --expires 4h Which DB?"
        self.assertTrue(self.post(sid, cmd, rec("ask", b.id)))
        r = self.receipt("ask", b.id)
        self.assertTrue(receipts.matches(r, {"kind": "ask", "id": str(b.id), "rev": 1, "question": "Which DB?",
                                             "until": drive_authz.canonical_until(b.until), "default": "sqlite"}))

    def test_the_until_of_an_ask_is_utc_whatever_zone_the_board_answers_in(self):
        import datetime as dt
        utc = dt.datetime(2026, 10, 11, 12, 0, tzinfo=dt.timezone.utc)
        west = utc.astimezone(dt.timezone(dt.timedelta(hours=-7)))
        self.assertNotEqual(west.isoformat(), utc.isoformat())
        self.assertEqual(drive_authz.canonical_until(west), "2026-10-11T12:00:00+00:00")
        self.assertEqual(drive_authz.canonical_until(utc), drive_authz.canonical_until(west))
        self.assertEqual(drive_authz.canonical_until(dt.datetime(2026, 10, 11, 12, 0)), "2026-10-11T12:00:00+00:00")
        self.assertIsNone(drive_authz.canonical_until(None))

    def test_ask_whose_default_or_text_differs_gets_no_receipt(self):
        sid, _ = self.launch_seat("eng-a")
        b = self.open_question(sid, default="postgres")
        self.assertFalse(self.post(sid, f"swarm ask --job J --key {sid} --to human --default sqlite Which DB?",
                                   rec("ask", b.id)))
        b = self.open_question(sid, text="Another question?")
        self.assertFalse(self.post(sid, f"swarm ask --job J --key {sid} --to human --default sqlite Which DB?",
                                   rec("ask", b.id)))

    def test_ask_by_another_agent_gets_no_receipt(self):
        sid, _ = self.launch_seat("eng-a")
        other, _ = self.launch_seat("eng-b")
        b = self.open_question(other)
        self.assertFalse(self.post(sid, f"swarm ask --job J --key {sid} --to human --default sqlite Which DB?",
                                   rec("ask", b.id)))

    def test_answer_is_receipted_for_the_seat_and_for_the_requester(self):
        sid, _ = self.launch_seat("eng-a")
        b = self.open_question(sid, to="eng-a")
        with self.board() as bd:
            bd.resolve_blocker(b.id, "sqlite", actor=bd.active_agent_name(sid))
        self.assertTrue(self.post(sid, f"swarm answer {b.id} --key {sid} sqlite", rec("answer", b.id)))
        r = self.receipt("answer", b.id)
        self.assertTrue(receipts.matches(r, {"kind": "answer", "id": str(b.id), "rev": 1, "ask_id": str(b.id),
                                             "answer": "sqlite"}))

    def test_requester_answer_is_receipted(self):
        sid, _ = self.launch_seat("eng-a")
        b = self.open_question(sid)
        with self.board() as bd:
            bd.resolve_blocker(b.id, "postgres", actor="human")
        self.assertTrue(self.post(REQ, f"swarm answer Q{b.id} postgres", rec("answer", b.id)))
        self.assertEqual(self.receipt("answer", b.id).action_id, "requester:answer")

    def test_an_answer_that_differs_from_the_resolution_gets_no_receipt(self):
        sid, _ = self.launch_seat("eng-a")
        b = self.open_question(sid)
        with self.board() as bd:
            bd.resolve_blocker(b.id, "postgres", actor="human")
        self.assertFalse(self.post(REQ, f"swarm answer {b.id} sqlite", rec("answer", b.id)))
        self.assertIn("not answered as submitted", self.failures())

    def test_a_comment_is_not_an_answer_and_is_no_failure(self):
        sid, _ = self.launch_seat("eng-a")
        b = self.open_question(sid)
        with self.board() as bd:
            bd.resolve_blocker(b.id, "postgres", actor="human")
        self.assertFalse(self.post(REQ, f"swarm answer {b.id} --comment hello", rec("answer", b.id)))
        self.assertEqual(self.failures(), "")

    def test_default_option_and_reopen_answers(self):
        sid, _ = self.launch_seat("eng-a")
        b = self.open_question(sid, default="sqlite")
        with self.board() as bd:
            bd.resolve_blocker(b.id, "sqlite", actor="human")
        self.assertTrue(self.post(REQ, f"swarm answer {b.id} --default", rec("answer", b.id)))






class PersonReceipts(DriveCase):
    def test_requester_person_command_is_receipted(self):
        for sub in ("halt", "unhalt", "deactivate"):
            with self.subTest(sub=sub):
                self.assertTrue(self.post(REQ, f"swarm {sub} --job J", rec(sub, 9)))
                r = self.receipt("person", 9)
                self.assertTrue(receipts.matches(r, {"kind": "person", "id": "9", "rev": 1, "action": sub,
                                                     "target": "J"}))
                self.assertEqual(r.action_id, f"requester:{sub}")
                self.assertEqual((r.seat, r.attempt), (None, 0))

    def test_foreign_deactivate_is_not_receipted(self):
        self.assertFalse(self.post("someone-else", "swarm deactivate --job J", rec("deactivate", 9)))
        self.assertEqual(self.failures(), "")

    def test_a_seat_session_cannot_take_the_person_path(self):
        sid, _ = self.launch_seat("lead", role="engineering_lead", kind="lead")
        for sub in ("halt", "unhalt", "deactivate", "activate"):
            self.assertFalse(self.post(sid, f"swarm {sub} --job J", rec(sub, 9)))

    def test_the_record_kind_must_be_the_command(self):
        self.assertFalse(self.post(REQ, "swarm unhalt --job J", rec("halt", 9)))

    def test_requester_of_another_job_is_not_the_requester_of_this_one(self):
        self.assertFalse(self.post(REQ, "swarm halt --job K", rec("halt", 9)))

    def test_a_terminal_ledger_still_receipts_the_requesters_deactivate(self):
        self.edit(lambda l: setattr(l, "terminal", {"verdict": "unreachable", "at": ISO}))
        self.assertTrue(self.post(REQ, "swarm deactivate --job J", rec("deactivate", 9)))

    def test_activate_follows_the_activation_order(self):
        """[RR1a]: the owner record names the session, the board carries the claim, the ledger names the requester."""
        self.assertTrue(self.post(REQ, "swarm activate --job J --goal G", rec("activate", 3)))
        self.assertEqual(self.receipt("person", 3).action_id, "requester:activate")

    def test_activate_without_the_owner_record_or_the_board_claim_is_not_receipted(self):
        with self.board() as b:
            b.set_job_data("J", "drive.enabled", "0")
        self.assertFalse(self.post(REQ, "swarm activate --job J --goal G", rec("activate", 3)))
        with self.board() as b:
            b.set_job_data("J", "drive.enabled", "1")
        enrolment.write_job_owner(self.key, job="J", harness="claude", session_id="another", cwd=str(self.work))
        self.assertFalse(self.post(REQ, "swarm activate --job J --goal G", rec("activate", 3)))

    def test_requester_receipt_failure_is_logged_and_has_no_kick(self):
        with mock.patch.object(receipts, "write", side_effect=OSError("disk gone")):
            self.assertFalse(self.post(REQ, "swarm unhalt --job J", rec("unhalt", 9)))
        self.assertIn("disk gone", self.failures())
        self.kick.assert_not_called()


class FailureLog(DriveCase):
    def test_log_is_one_printable_line(self):
        drivegate.log_receipt_failure("J person id=9 rev=1: OSError: x\nINJECTED line")
        p = paths.host_dir() / drivegate.RECEIPT_FAILURES
        self.assertEqual(len(p.read_text().splitlines()), 1)
        self.assertIn("\\x0aINJECTED", p.read_text())

    @posix_only("Windows has no file modes")
    def test_P10_log_is_private(self):
        drivegate.log_receipt_failure("J person id=9 rev=1: OSError: x")
        p = paths.host_dir() / drivegate.RECEIPT_FAILURES
        self.assertEqual(oct(p.stat().st_mode & 0o777), "0o600")
        self.assertEqual(oct(p.parent.stat().st_mode & 0o777), "0o700")

    def test_log_is_cut_back_to_its_tail_when_it_grows(self):
        d = paths.host_dir()
        d.mkdir(parents=True, exist_ok=True)
        (d / drivegate.RECEIPT_FAILURES).write_text("1000 old line\n" * 30000)
        drivegate.log_receipt_failure("J fresh failure")
        text = (d / drivegate.RECEIPT_FAILURES).read_text()
        self.assertLess(len(text), drivegate.RECEIPT_LOG_KEEP + 1024)
        self.assertTrue(text.rstrip().endswith("J fresh failure"))
        self.assertTrue(text.startswith("1000 old line\n"))      # cut on a line boundary

    def test_logging_never_raises(self):
        with mock.patch("swarm.safefs.dir_fd", side_effect=OSError("no dir")):
            drivegate.log_receipt_failure("x")


class CodexReceipts(DriveCase):
    """[F18] Codex hooks write receipts exactly like Claude's: tool_response is the command's output as a plain
    string, and the root thread's payload has no agent_id."""

    def test_codex_done_receipted(self):
        sid, _ = self.launch_seat("eng-a", harness="codex")
        rid = self.post_as(sid, f'DONE "{ART}" | x')
        payload = {"session_id": sid, "turn_id": "t1", "model": "gpt-5.6-sol", "cwd": str(self.work),
                   "hook_event_name": "PostToolUse", "tool_name": "Bash",
                   "tool_input": {"command": f"swarm done --job J --artifact {ART} --summary x"},
                   "tool_response": f"posted #{rid}\n{rec('done', rid)}\n"}
        self.assertTrue(drivegate.post_tool(self.cfg, payload, hosts.get("codex")))
        self.assertEqual(self.receipt("done", rid).seat, "eng-a")

    def test_codex_done_through_the_hook_entry_point(self):
        """The whole hook: `swarm hook done` with a Codex payload reaches the receipt."""
        sid, _ = self.launch_seat("eng-a", harness="codex")
        rid = self.post_as(sid, f'DONE "{ART}" | x')
        self.hook("done", agent_id=None, session=sid, host="codex", cwd=str(self.work), tool_name="Bash",
                  tool_input={"command": f"swarm done --job J --artifact {ART} --summary x"},
                  tool_response=f"posted #{rid}\n{rec('done', rid)}\n")
        self.assertIsNotNone(self.receipt("done", rid))

    def test_claude_done_through_the_hook_entry_point(self):
        sid, _ = self.launch_seat("eng-a")
        rid = self.post_as(sid, f'DONE "{ART}" | x')
        self.hook("done", agent_id=None, session=sid, cwd=str(self.work), tool_name="Bash",
                  tool_input={"command": f"swarm done --job J --artifact {ART} --summary x"},
                  tool_response={"interrupted": False, "isImage": False, "noOutputExpected": False, "stderr": "",
                                 "stdout": f"posted #{rid}\n{rec('done', rid)}\n"})
        self.assertIsNotNone(self.receipt("done", rid))

    def test_a_subagent_payload_through_the_entry_point_gets_no_receipt(self):
        sid, _ = self.launch_seat("eng-a")
        rid = self.post_as(sid, f'DONE "{ART}" | x')
        self.hook("done", agent_id="sub-1", session=sid, cwd=str(self.work), tool_name="Bash",
                  tool_input={"command": f"swarm done --job J --artifact {ART} --summary x"},
                  tool_response={"stdout": f"{rec('done', rid)}\n"})
        self.assertIsNone(self.receipt("done", rid))


class PerCallCost(DriveCase):
    def test_a_command_that_is_not_swarm_never_opens_the_board(self):
        sid, _ = self.launch_seat("eng-a")
        with mock.patch.object(drivegate, "_open_board", side_effect=AssertionError("opened the board")):
            self.assertFalse(self.post(sid, "ls -la", rec("done", 3)))
            self.assertFalse(self.post(sid, "swarm status --job J", None))     # no record line
            self.assertFalse(drivegate.post_tool(self.cfg, self.payload(sid, "Read", None), self.host))
        with mock.patch.object(drivegate, "record_calls", side_effect=AssertionError("tokenized")):
            self.assertFalse(self.post(sid, "ls -la", rec("done", 3)))      # no `swarm` in it: not even parsed

    def test_a_host_without_a_drive_ledger_does_nothing(self):
        with mock.patch.object(drivegate, "has_ledgers", return_value=False), \
                mock.patch.object(drivegate, "_open_board", side_effect=AssertionError("opened the board")):
            self.assertFalse(self.post("any", "swarm done --job J --artifact x@1234567", rec("done", 3)))
