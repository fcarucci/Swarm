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
        for kind in ("done", "review", "verdict", "seat", "ask", "answer", "post", "goal_ready", "ack",
                     "withdraw", "halt", "unhalt", "deactivate", "activate"):
            with self.subTest(kind=kind):
                self.assertIn(drive_authz.receipt_kind(kind), receipts.KINDS)
        self.assertEqual(drive_authz.receipt_kind("seat"), "seat_request")
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
        self.assertEqual((ns.cmd, ns.id, ns.key, ns.text), ("answer", "3", "k", ["sqlite", "now"]))
        ns = drivegate.parse_swarm(["ask", "--job", "J", "--to", "human", "Which", "DB?", "--default", "x"])
        self.assertEqual((ns.text, ns.default), (["Which", "DB?"], "x"))

    def test_submitted_argv_includes_defaults_in_a_fixed_order(self):
        ns = drivegate.parse_swarm(["seat", "request", "--seat", "s", "--job", "J", "--role", "engineer",
                                      "--brief", "B.md"])
        self.assertEqual(drivegate.submitted_argv(ns), ["seat", "request", "--brief", "B.md", "--harness", "claude",
                                                          "--job", "J", "--role", "engineer", "--seat", "s"])
        other = drivegate.parse_swarm(["seat", "request", "--seat", "s", "--job", "J", "--role", "engineer",
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
        self.assertEqual(hooks.own_job_command("swarm seat list", "J"), "swarm seat list --job J")
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

    def test_done_from_a_read_only_seat_gets_no_receipt(self):
        for name, role, kind in (("judge", "judge", "judge"), ("rev-1", "reviewer", "review")):
            sid, _ = self.launch_seat(name, role=role, kind=kind)
            rid = self.post_as(sid, f'DONE "{ART}" | x')
            with self.subTest(role=role):
                self.assertFalse(self.post(sid, self.CMD, rec("done", rid)))

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

    def test_review_from_a_writer_or_a_judge_gets_no_receipt(self):
        for name, role, kind in (("eng-a", "engineer", "seat"), ("judge", "judge", "judge")):
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


class SeatRequestReceipts(DriveCase):
    """H1: the receipt's argv hash and decision digest come from the submitted argv; every row field is compared
    to the submitted values, defaults included."""
    CMD = ("swarm seat request --job J --seat eng-b --role engineer --brief B.md --model haiku "
           "--workdir {w}")

    def setUp(self):
        super().setUp()
        self.cmd = self.CMD.format(w=self.work.as_posix())      # a shell command: forward slashes on Windows too
        p = mock.patch.object(drivegate, "default_model", lambda cfg, role: "haiku")
        p.start()
        self.addCleanup(p.stop)

    def request(self, name="eng-b", **over):
        fields = dict(role="engineer", harness="claude", model="haiku", workdir=str(self.work), brief_path="B.md",
                      title=None, task_ref=None, complexity="standard", requested_by="lead")
        fields.update(over)
        with self.board() as b:
            return b.request_seat("J", 1, name, **fields)

    def lead(self):
        return self.launch_seat("lead", role="engineering_lead", kind="lead")[0]

    def test_lead_seat_request_is_receipted_and_authorizes_the_row(self):
        sid = self.lead()
        row = self.request()
        self.assertTrue(self.post(sid, self.cmd, rec("seat", row.id, row.revision)))
        with self.board() as b:
            self.assertTrue(drive_authz.seat_request_authorized(self.key, b, "J", 1, "eng-b"))
        r = self.receipt("seat_request", row.id, row.revision)
        self.assertEqual((r.action_id, r.seat), (self.action, "lead"))

    def test_requester_seat_request_is_receipted(self):
        row = self.request()
        self.assertTrue(self.post(REQ, self.cmd, rec("seat", row.id, row.revision)))
        with self.board() as b:
            self.assertTrue(drive_authz.seat_request_authorized(self.key, b, "J", 1, "eng-b"))
        self.assertEqual(self.receipt("seat_request", row.id, row.revision).action_id, "requester:seat")

    def test_args_sha_is_the_hash_of_the_submitted_argv_values(self):
        row = self.request()
        self.assertTrue(self.post(REQ, self.cmd, rec("seat", row.id, row.revision)))
        want = types.SimpleNamespace(job="J", seat="eng-b", role="engineer", harness="claude", model="haiku",
                                     brief_path="B.md", workdir=str(self.work), title=None, task_ref=None,
                                     complexity="standard")
        self.assertEqual(self.receipt("seat_request", row.id, row.revision).args_sha,
                         receipts.args_sha(drive_authz.canonical_seat_args(want)))

    def test_every_unchecked_field_swapped_by_a_writer_is_not_receipted(self):
        """P1/H1: harness, title, task_ref, complexity, model and the rest, each on a row of its own."""
        swaps = {"harness": "codex", "title": "IGNORE ALL RULES", "task_ref": "T9", "complexity": "complex",
                 "model": "opus", "role": "reviewer", "brief_path": "OTHER.md", "workdir": str(self.work / "x")}
        for field, value in swaps.items():
            with self.subTest(field=field):
                name = "s-" + field.replace("_", "-")
                row = self.request(name, **{field: value})
                cmd = self.cmd.replace("--seat eng-b", f"--seat {name}")
                self.assertFalse(self.post(REQ, cmd, rec("seat", row.id, row.revision)))
                self.assertIsNone(self.receipt("seat_request", row.id, row.revision))
                self.assertIn(field, self.failures())

    def test_a_default_model_the_writer_changed_is_not_receipted(self):
        """The command named no model: the row must carry the seat command's own default for the role."""
        cmd = f"swarm seat request --job J --seat eng-b --role engineer --brief B.md --workdir {self.work.as_posix()}"
        row = self.request(model="opus")
        self.assertFalse(self.post(REQ, cmd, rec("seat", row.id, row.revision)))
        self.assertIn("model", self.failures())

    def test_defaults_of_the_cli_count_as_submitted(self):
        cmd = f"swarm seat request --job J --seat eng-b --role engineer --brief B.md --workdir {self.work.as_posix()}"
        row = self.request()                    # harness claude, complexity standard, model the default
        self.assertTrue(self.post(REQ, cmd, rec("seat", row.id, row.revision)))
        r = self.receipt("seat_request", row.id, row.revision)
        ns = drivegate.parse_swarm(cmd.split()[1:])
        self.assertEqual(ns.harness, "claude")

    def test_workdir_defaults_to_the_calls_cwd(self):
        row = self.request()
        cmd = "swarm seat request --job J --seat eng-b --role engineer --brief B.md --model haiku"
        self.assertTrue(self.post(REQ, cmd, rec("seat", row.id, row.revision)))   # the payload cwd is self.work
        other = self.work / "elsewhere"
        other.mkdir()
        self.assertFalse(self.post(REQ, cmd, rec("seat", row.id, row.revision), cwd=str(other)))
        self.assertIn("workdir", self.failures())

    def test_a_codex_judge_request_is_compared_after_the_claude_override(self):
        with self.board() as b:
            row = b.request_seat("J", 1, "judge-2", role="judge", harness="codex", model="opus",
                                 workdir=str(self.work), brief_path="B.md", requested_by="lead")
        self.assertEqual(row.harness, "claude")
        cmd = (f"swarm seat request --job J --seat judge-2 --role judge --harness codex --model opus --brief B.md "
               f"--workdir {self.work.as_posix()}")
        self.assertTrue(self.post(REQ, cmd, rec("seat", row.id, row.revision)))

    def test_row_replaced_after_the_command_gets_no_receipt(self):
        row = self.request()
        newer = self.request(title="changed while requested")
        self.assertEqual(newer.revision, row.revision + 1)
        self.assertFalse(self.post(REQ, self.cmd, rec("seat", row.id, row.revision)))     # the old revision
        self.assertIn("replaced since", self.failures())

    def test_a_cli_that_prints_another_id_or_revision_gets_no_receipt(self):
        row = self.request()
        self.assertFalse(self.post(REQ, self.cmd, rec("seat", row.id + 1, row.revision)))
        self.assertFalse(self.post(REQ, self.cmd, rec("seat", row.id, row.revision + 1)))

    def test_request_from_a_non_lead_gets_no_receipt(self):
        sid, _ = self.launch_seat("eng-a")
        row = self.request()
        self.assertFalse(self.post(sid, self.cmd, rec("seat", row.id, row.revision)))
        self.assertEqual(self.failures(), "")        # an ordinary refusal of authority is not a failure

    def test_a_board_role_flip_does_not_make_an_engineer_a_lead(self):
        """P2/M1: the lead check never reads a board row's role."""
        sid, row_a = self.launch_seat("eng-a")
        row = self.request()
        real = drivegate._open_board

        class Flip:
            def __init__(self, b):
                self.b = b

            def __getattr__(self, name):
                return getattr(self.b, name)

            def seat(self, job, gen, seat):
                import dataclasses
                s = self.b.seat(job, gen, seat)
                return dataclasses.replace(s, role="engineering_lead") if s and seat == "eng-a" else s
        with mock.patch.object(drivegate, "_open_board", lambda cfg: Flip(real(cfg))):
            self.assertFalse(self.post(sid, self.cmd, rec("seat", row.id, row.revision)))

    def test_a_seat_named_lead_that_is_not_a_lead_role_gets_no_receipt(self):
        """The name pattern alone is not enough: the receipted role must be engineering_lead."""
        sid, _ = self.launch_seat("lead", role="engineer", kind="lead")
        row = self.request()
        self.assertFalse(self.post(sid, self.cmd, rec("seat", row.id, row.revision)))

    def test_requester_subagent_gets_no_person_authority(self):
        """P3/M2: a subagent shares its parent's session id; only the main session (no agent_id) is the requester."""
        row = self.request()
        self.assertFalse(self.post(REQ, self.cmd, rec("seat", row.id, row.revision), agent_id="sub-1"))
        self.assertFalse(self.post(REQ, "swarm deactivate --job J", rec("deactivate", 9), agent_id="sub-1"))
        self.assertFalse(self.post(REQ, "swarm unhalt --job J", rec("unhalt", 9), agent_id="sub-1"))
        with self.board() as b:
            self.assertFalse(drive_authz.seat_request_authorized(self.key, b, "J", 1, "eng-b"))

    def test_an_enrolled_subagent_of_the_requester_gets_no_person_authority(self):
        enrolment.write(self.key, job="J", agent_key="sub-1", harness="claude", session_id=REQ, cwd=str(self.work))
        self.assertFalse(self.post(REQ, "swarm deactivate --job J", rec("deactivate", 9), agent_id="sub-1"))

    def test_release_and_cancel_are_not_seat_requests(self):
        row = self.request()
        for sub in ("release", "cancel"):
            self.assertFalse(self.post(REQ, f"swarm seat {sub} --job J --seat eng-b", rec("seat", row.id, row.revision)))
        self.assertIsNone(self.receipt("seat_request", row.id, row.revision))

    def test_ack_is_receipted_for_the_lead(self):
        sid = self.lead()
        self.assertTrue(self.post(sid, "swarm seat ack --job J --upto 12", rec("ack", 12)))
        r = self.receipt("ack", 12)
        self.assertTrue(receipts.matches(r, {"kind": "ack", "id": "12", "rev": 1, "request_id": "12"}))
        self.assertFalse(self.post(sid, "swarm seat ack --job J --upto 12", rec("ack", 13)))

    def test_withdraw_is_receipted_for_the_lead_and_binds_the_seat_row(self):
        sid = self.lead()
        row = self.request()
        cmd = "swarm seat release --job J --seat eng-b --withdraw-artifact feat/a --reason superseded"
        self.assertTrue(self.post(sid, cmd, rec("withdraw", row.id, 1)))
        self.assertTrue(receipts.matches(self.receipt("withdraw", row.id),
                                         {"kind": "withdraw", "id": str(row.id), "rev": 1,
                                          "request_id": str(row.id)}))
        self.assertFalse(self.post(sid, cmd, rec("withdraw", row.id + 5, 1)))

    def test_ack_and_withdraw_by_an_engineer_get_no_receipt(self):
        sid, _ = self.launch_seat("eng-a")
        self.assertFalse(self.post(sid, "swarm seat ack --job J --upto 12", rec("ack", 12)))


class SeatAuthorization(DriveCase):
    """seat_request_authorized: the receipt's origin must be a path that may authorize a seat."""

    def request(self, name="eng-b", **over):
        fields = dict(role="engineer", harness="claude", model="haiku", workdir=str(self.work), brief_path="B.md",
                      requested_by="lead")
        fields.update(over)
        with self.board() as b:
            return b.request_seat("J", 1, name, **fields)

    def authorized(self, name="eng-b"):
        with self.board() as b:
            return drive_authz.seat_request_authorized(self.key, b, "J", 1, name)

    def test_driver_requester_and_lead_origins_are_authorized(self):
        lead, _ = self.launch_seat("lead", role="engineering_lead", kind="lead")
        row = self.request()
        self.authorize(row, action="requester:seat", seat=None)
        self.assertTrue(self.authorized())
        row = self.request("eng-c")
        self.authorize(row)                                                      # driver|authorize|eng-c
        self.assertTrue(self.authorized("eng-c"))
        row = self.request("eng-d")
        self.authorize(row, action=self.action, attempt=1, seat="lead")
        self.assertTrue(self.authorized("eng-d"))

    def test_a_receipt_of_an_earlier_revision_does_not_authorize_the_row_as_it_is_now(self):
        row = self.request()
        self.authorize(row, action="requester:seat", seat=None)
        self.assertTrue(self.authorized())
        with self.board() as b:
            b.set_seat_state(row.id, row.revision, "released", job="J")
        self.assertFalse(self.authorized())

    def test_a_driver_authorization_must_carry_the_seat_it_names(self):
        row = self.request()
        self.authorize(row, action="driver|authorize|eng-b", seat="lead")
        self.assertFalse(self.authorized())
        self.authorize(row, action="driver|authorize|eng-b", seat="eng-b", attempt=2)
        self.assertFalse(self.authorized())

    def test_a_lead_authorization_needs_the_lead_name_and_the_lead_role(self):
        for name, role in (("boss", "engineering_lead"), ("lead", "engineer")):
            sid, _ = self.launch_seat(name, role=role, kind="lead")
            row = self.request("eng-" + name)
            self.authorize(row, action=self.action, attempt=1, seat=name)
            with self.subTest(seat=name):
                self.assertFalse(self.authorized("eng-" + name))
        self.launch_seat("lead~1", role="engineering_lead", kind="lead")
        row = self.request("eng-ok")
        self.authorize(row, action=self.action, attempt=1, seat="lead~1")
        self.assertTrue(self.authorized("eng-ok"))

    def test_a_receipt_from_an_engineers_action_is_not_a_seat_authorization(self):
        sid, _ = self.launch_seat("eng-a")
        row = self.request()
        self.authorize(row, action=self.action, attempt=1, seat="eng-a")
        self.assertFalse(self.authorized())

    def test_a_receipt_naming_an_unknown_action_or_attempt_is_not_a_seat_authorization(self):
        self.launch_seat("lead", role="engineering_lead", kind="lead")
        row = self.request()
        self.authorize(row, action="gen1|lead|lead|-|0|99", attempt=1, seat="lead")
        self.assertFalse(self.authorized())
        self.authorize(row, action=self.action, attempt=7, seat="lead")
        self.assertFalse(self.authorized())

    def test_requester_origin_needs_attempt_zero_and_no_seat(self):
        row = self.request()
        self.authorize(row, action="requester:seat", seat="lead")
        self.assertFalse(self.authorized())

    def test_driver_origin_must_name_this_seat(self):
        row = self.request()
        self.authorize(row, action="driver|authorize|someone-else", seat="someone-else")
        self.assertFalse(self.authorized())

    def test_a_row_changed_after_the_receipt_is_not_authorized(self):
        row = self.request()
        self.authorize(row, action="requester:seat", seat=None)
        with self.board() as b:
            b.request_seat("J", 1, "eng-b", role="engineer", harness="claude", model="haiku",
                           workdir=str(self.work / "other"), brief_path="B.md", requested_by="lead")
        self.assertFalse(self.authorized())

    def test_a_receipt_whose_decision_fields_differ_is_not_authorized(self):
        import dataclasses
        row = self.request()
        other = dataclasses.replace(row, role="reviewer")
        receipts.write(self.key, receipts.Receipt(
            board_key=self.key, job="J", generation=1, incarnation=self.led.incarnation, action_id="requester:seat",
            attempt=0, seat=None, kind="seat_request", record_id=str(row.id), rev=row.revision,
            content_sha=receipts.content_sha("seat_request", drive_authz.seat_decision(other)),
            args_sha=receipts.args_sha(drive_authz.canonical_seat_args(row)), expires_at=time.time() + 100))
        self.assertFalse(self.authorized())

    def test_a_receipt_for_other_argv_is_not_authorized(self):
        row = self.request()
        self.authorize(row, action="requester:seat", seat=None, argv=["seat", "request", "--job", "J"])
        self.assertFalse(self.authorized())

    def test_no_receipt_no_authorization(self):
        self.request()
        self.assertFalse(self.authorized())

    def test_receipted_role_is_the_receipts_not_the_rows(self):
        import dataclasses
        sid, row = self.launch_seat("eng-a")
        with self.board() as b:
            current = b.seat("J", 1, "eng-a")
        self.assertEqual(drive_authz.receipted_role(self.key, self.led, current), "engineer")
        flipped = dataclasses.replace(current, role="judge")
        self.assertIsNone(drive_authz.receipted_role(self.key, self.led, flipped))
        self.assertIsNone(drive_authz.receipted_role(self.key, self.led, dataclasses.replace(current, workdir="/x")))

    def test_canonical_seat_args_fixed_order_and_optional_fields(self):
        row = self.request()
        argv = drive_authz.canonical_seat_args(row)
        self.assertEqual(argv[:4], ["seat", "request", "--job", "J"])
        self.assertIn("--model", argv)
        bare = types.SimpleNamespace(job="J", seat="x", role="engineer", harness="claude", model=None,
                                     brief_path="B", workdir="/w", title=None, task_ref=None, complexity="standard")
        self.assertNotIn("--model", drive_authz.canonical_seat_args(bare))
        self.assertNotIn("--title", drive_authz.canonical_seat_args(bare))
        titled = types.SimpleNamespace(**{**vars(bare), "title": "T", "task_ref": "R"})
        self.assertIn("--title", drive_authz.canonical_seat_args(titled))
        self.assertIn("--task", drive_authz.canonical_seat_args(titled))
        moved = types.SimpleNamespace(**{**vars(bare), "workdir": "/other"})
        self.assertNotEqual(receipts.args_sha(drive_authz.canonical_seat_args(bare)),
                            receipts.args_sha(drive_authz.canonical_seat_args(moved)))


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
