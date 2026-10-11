"""Drive control commands (Task 6b): halt/unhalt, seat request/list/show/release/cancel, review,
goal-ready. Handlers run through the real CLI parser against the memory board, under a temporary HOME
inside the suite sandbox. The lead's agent key is patched in (a seat gets its key only from a launch
claim, which these tests do not run)."""
from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import SANDBOX, MemoryHarness, home_env, posix_only  # noqa: F401

from swarm import cli, drive_cli, enrolment  # noqa: E402
from swarm.board.autoinit import store_key  # noqa: E402
from swarm.supervisor import ledger  # noqa: E402

JOB = "J"
REQ_SESSION = "REQ-SESSION"
LEAD_KEY = "k-lead"
ENG_KEY = "k-eng"


class ControlCase(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(dir=SANDBOX, prefix="seatcli-home-"))
        self.work = self.home / "work"
        self.work.mkdir()
        p = mock.patch.dict(os.environ, home_env(self.home))
        p.start()
        self.addCleanup(p.stop)
        self.h = MemoryHarness()
        self.h.reset()
        self.board = self.h.board()
        self.addCleanup(self.board.close)
        self.cfg = self.h.cfg
        self.bkey = store_key(self.cfg)
        self.board.open_job(JOB, "d", None, REQ_SESSION, "Francesco", goal="g")
        ledger.create(self.bkey, JOB, budgets={}, activated_at=self.board.now().isoformat())
        enrolment.write_job_owner(self.bkey, job=JOB, harness="claude", session_id=REQ_SESSION,
                                  cwd=str(self.work))
        self.roots = [str(self.work)]
        tmp = mock.patch.object(drive_cli, "TMP_ROOTS", ("/tmp-not-used-by-tests",))
        tmp.start()
        self.addCleanup(tmp.stop)
        for target, fn in (("_owner_roots", lambda bk, job: list(self.roots)),
                           ("_lead_keys", lambda board, job, gen: {LEAD_KEY})):
            patch = mock.patch.object(drive_cli, target, side_effect=fn)
            patch.start()
            self.addCleanup(patch.stop)
        self.set_session(REQ_SESSION)

    def set_session(self, sid):
        p = mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": sid})
        p.start()
        self.addCleanup(p.stop)

    def enrol(self, session, key):
        enrolment.write(self.bkey, job=JOB, agent_key=key, harness="claude", session_id=session,
                        cwd=str(self.work))

    def run_cmd(self, *argv):
        """Parse `argv` with the real CLI parser and run its handler: (rc, stdout, stderr)."""
        args = cli._parser().parse_args(list(argv))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.BOARD_COMMANDS[args.cmd](self.board, self.cfg, args) or 0
        return rc, out.getvalue(), err.getvalue()

    def request_seat(self, seat="eng", role="engineer", workdir=None):
        return self.run_cmd("seat", "request", "--job", JOB, "--seat", seat, "--role", role,
                            "--brief", "B.md", "--workdir", str(workdir or self.work))

    @staticmethod
    def last_line(out):
        return out.rstrip("\n").splitlines()[-1]


class Halt(ControlCase):
    def test_halt_by_requester_writes_ledger_and_prints_record(self):
        rc, out, err = self.run_cmd("halt", "--job", JOB, "--reason", "wait for review")
        self.assertEqual(rc, 0, err)
        led = ledger.load(self.bkey, JOB)
        self.assertEqual(led.halted["reason"], "wait for review")
        self.assertTrue(led.halted["by"].startswith("person"))
        self.assertRegex(self.last_line(out), r"^swarm-record: kind=halt id=\d+ rev=1$")

    def test_halt_refused_for_enrolled_agent(self):
        self.enrol("ENG-SESSION", ENG_KEY)
        self.set_session("ENG-SESSION")
        rc, _, err = self.run_cmd("halt", "--job", JOB)
        self.assertNotEqual(rc, 0)
        self.assertIn("enrolled", err)
        self.assertIsNone(ledger.load(self.bkey, JOB).halted)

    def test_halt_twice_refused(self):
        self.assertEqual(self.run_cmd("halt", "--job", JOB)[0], 0)
        rc, _, err = self.run_cmd("halt", "--job", JOB)
        self.assertNotEqual(rc, 0)
        self.assertIn("already halted", err)

    def test_unhalt_adds_halted_time_and_clears_halt(self):
        self.run_cmd("halt", "--job", JOB, "--reason", "r")
        rc, out, err = self.run_cmd("unhalt", "--job", JOB)
        self.assertEqual(rc, 0, err)
        led = ledger.load(self.bkey, JOB)
        self.assertIsNone(led.halted)
        self.assertGreaterEqual(led.halted_total_seconds, 0.0)
        self.assertRegex(self.last_line(out), r"^swarm-record: kind=unhalt id=\d+ rev=1$")

    def test_unhalt_counts_the_time_halted(self):
        import datetime as dt
        self.run_cmd("halt", "--job", JOB, "--reason", "r")
        led = ledger.load(self.bkey, JOB)
        led.halted["at"] = (self.board.now() - dt.timedelta(hours=1)).isoformat()
        led.save()
        self.assertEqual(self.run_cmd("unhalt", "--job", JOB)[0], 0)
        self.assertGreaterEqual(ledger.load(self.bkey, JOB).halted_total_seconds, 3500.0)

    def test_unhalt_when_not_halted_refused(self):
        rc, _, err = self.run_cmd("unhalt", "--job", JOB)
        self.assertNotEqual(rc, 0)
        self.assertIn("not halted", err)

    def test_unhalt_refused_for_enrolled_agent(self):
        self.run_cmd("halt", "--job", JOB)
        self.enrol("ENG-SESSION", ENG_KEY)
        self.set_session("ENG-SESSION")
        rc, _, _ = self.run_cmd("unhalt", "--job", JOB)
        self.assertNotEqual(rc, 0)
        self.assertIsNotNone(ledger.load(self.bkey, JOB).halted)

    def test_halt_without_drive_ledger_refused(self):
        self.board.open_job("K", "d", None, REQ_SESSION, "Francesco", goal="g")
        rc, _, err = self.run_cmd("halt", "--job", "K")
        self.assertNotEqual(rc, 0)
        self.assertIn("not a drive job", err)

    def test_control_commands_are_registered(self):
        for name in ("halt", "unhalt", "seat", "review", "goal-ready"):
            self.assertIn(name, cli.BOARD_COMMANDS)
        self.assertEqual(cli._parser().parse_args(["halt", "--job", JOB]).cmd, "halt")


class Seats(ControlCase):
    def test_seat_request_by_requester_prints_record_last(self):
        rc, out, err = self.request_seat()
        self.assertEqual(rc, 0, err)
        row = self.board.seat(JOB, 1, "eng")
        self.assertEqual((row.role, row.state, row.model), ("engineer", "requested", "haiku"))
        self.assertEqual(self.last_line(out), f"swarm-record: kind=seat id={row.id} rev=1")

    def test_seat_request_from_lead_allowed(self):
        self.enrol("LEAD-SESSION", LEAD_KEY)
        self.set_session("LEAD-SESSION")
        self.assertEqual(self.request_seat()[0], 0)

    def test_seat_request_from_other_enrolled_agent_refused(self):
        self.enrol("ENG-SESSION", ENG_KEY)
        self.set_session("ENG-SESSION")
        rc, _, err = self.request_seat()
        self.assertNotEqual(rc, 0)
        self.assertIn("ask it on the board", err)
        self.assertIsNone(self.board.seat(JOB, 1, "eng"))

    def test_seat_request_outside_job_roots_refused(self):
        other = self.home / "other"
        other.mkdir()
        rc, _, err = self.request_seat(workdir=other)
        self.assertNotEqual(rc, 0)
        self.assertIn("outside the job's work roots", err)

    @posix_only("the /tmp refusal is about POSIX paths")
    def test_seat_request_under_tmp_refused(self):
        self.roots.append("/tmp/x")
        with mock.patch.object(drive_cli, "TMP_ROOTS", ("/tmp", "/private/tmp")):   # macOS: /tmp -> /private/tmp
            rc, _, err = self.request_seat(workdir="/tmp/x")
        self.assertNotEqual(rc, 0)
        self.assertIn("never /tmp", err)

    @posix_only("the /tmp refusal is about POSIX paths")
    def test_tmp_refusal_is_a_path_prefix_not_a_substring(self):
        with mock.patch.object(drive_cli, "TMP_ROOTS", ("/tmp", "/private/tmp")):
            self.assertTrue(drive_cli._under_tmp("/tmp/x"))
            self.assertFalse(drive_cli._under_tmp("/tmpx/x"))
            self.assertFalse(drive_cli._under_tmp("/home/u/tmp/x"))

    def test_seat_request_model_outside_allowlist_refused(self):
        rc, _, err = self.run_cmd("seat", "request", "--job", JOB, "--seat", "eng", "--role", "engineer",
                                  "--brief", "B.md", "--workdir", str(self.work), "--model", "gpt-9")
        self.assertNotEqual(rc, 0)
        self.assertIn("allowed_models", err)

    def test_seat_request_idempotent_same_row(self):
        self.request_seat()
        first = self.board.seat(JOB, 1, "eng")
        rc, out, _ = self.request_seat()
        self.assertEqual(rc, 0)
        self.assertEqual(self.board.seat(JOB, 1, "eng").revision, first.revision)
        self.assertEqual(self.last_line(out), f"swarm-record: kind=seat id={first.id} rev=1")

    def test_seat_cancel_is_person_only(self):
        self.request_seat()
        self.enrol("ENG-SESSION", ENG_KEY)
        self.set_session("ENG-SESSION")
        self.assertNotEqual(self.run_cmd("seat", "cancel", "--job", JOB, "--seat", "eng")[0], 0)
        self.assertEqual(self.board.seat(JOB, 1, "eng").state, "requested")
        self.set_session(REQ_SESSION)
        rc, out, err = self.run_cmd("seat", "cancel", "--job", JOB, "--seat", "eng")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.board.seat(JOB, 1, "eng").state, "cancelled")
        self.assertRegex(self.last_line(out), r"^swarm-record: kind=seat id=\d+ rev=2$")

    def test_seat_release_withdraws_artifact_group(self):
        self.request_seat()
        rc, out, err = self.run_cmd("seat", "release", "--job", JOB, "--seat", "eng",
                                    "--withdraw-artifact", "feat/x", "--reason", "superseded")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.board.seat(JOB, 1, "eng").state, "released")
        self.assertEqual(ledger.load(self.bkey, JOB).integrated["feat/x"]["withdrawn"], "superseded")
        self.assertRegex(self.last_line(out), r"^swarm-record: kind=seat id=\d+ rev=2$")

    def test_seat_release_withdraw_needs_reason(self):
        self.request_seat()
        rc, _, err = self.run_cmd("seat", "release", "--job", JOB, "--seat", "eng",
                                  "--withdraw-artifact", "feat/x")
        self.assertNotEqual(rc, 0)
        self.assertIn("--reason", err)
        self.assertEqual(self.board.seat(JOB, 1, "eng").state, "requested")

    def test_seat_show_and_list_print_rows(self):
        self.request_seat()
        rc, out, _ = self.run_cmd("seat", "list", "--job", JOB)
        self.assertEqual(rc, 0)
        self.assertIn("eng", out)
        rc, out, _ = self.run_cmd("seat", "show", "--job", JOB, "--seat", "eng")
        self.assertIn("role: engineer", out)


class VerdictLine(ControlCase):
    def test_verdict_prints_record_line_with_its_message_id(self):
        judge = self.board.allocate_name("jk", JOB)
        self.board.claim_judge("jk", JOB)
        rc, out, err = self.run_cmd("verdict", "--job", JOB, "--as", judge, "met", "all requirements hold")
        self.assertEqual(rc, 0, err)
        verdict_id = next(m.id for m in self.board.messages_after(0, JOB)
                          if m.message.startswith("VERDICT met: "))
        self.assertEqual(self.last_line(out), f"swarm-record: kind=verdict id={verdict_id} rev=1")

    def test_goal_verdict_binds_candidate_and_its_message_id(self):
        judge = self.board.allocate_name("jk", JOB)
        self.board.claim_judge("jk", JOB)
        rc, out, err = self.run_cmd("verdict", "--job", JOB, "--as", judge, "--scope", "goal",
                                    "--candidate", "feat/x@abc1234", "--req", "d" * 64, "met", "all held")
        self.assertEqual(rc, 0, err)
        verdict_id = next(m.id for m in self.board.messages_after(0, JOB)
                          if m.message.startswith("VERDICT met: "))
        self.assertEqual(self.last_line(out), f"swarm-record: kind=verdict id={verdict_id} rev=1")
        gen = ledger.load(self.bkey, JOB).generation
        data = self.board.job_data(JOB)
        count = int(data[f"drive.verdict.{gen}.goal"])
        rec = json.loads("".join(data[f"drive.verdict.{gen}.goal.{i}"] for i in range(count)))
        self.assertEqual((rec["candidate"], rec["req"], rec["message_id"]), ("feat/x@abc1234", "d" * 64, verdict_id))
        self.assertIsNone(rec["artifact"])

    def test_a_verdict_from_a_non_judge_is_refused_and_posts_nothing(self):
        self.board.allocate_name("jk", JOB)
        self.board.claim_judge("jk", JOB)
        outsider = self.board.allocate_name("outsider", JOB)
        rc, _, err = self.run_cmd("verdict", "--job", JOB, "--as", outsider, "met", "all held")
        self.assertEqual(rc, 1)
        self.assertIn("is not the judge", err)
        self.assertEqual([m for m in self.board.messages_after(0, JOB) if m.message.startswith("VERDICT ")], [])

    def test_goal_verdict_is_not_bound_to_an_artifact_named_in_its_reason(self):
        judge = self.board.allocate_name("jk", JOB)
        self.board.claim_judge("jk", JOB)
        rc, _, err = self.run_cmd("verdict", "--job", JOB, "--as", judge, "--scope", "goal",
                                  "--candidate", "feat/x@abc1234", "--req", "d" * 64, "met",
                                  "feat/x@abc1234: all held")
        self.assertEqual(rc, 0, err)
        gen = ledger.load(self.bkey, JOB).generation
        data = self.board.job_data(JOB)
        count = int(data[f"drive.verdict.{gen}.goal"])
        rec = json.loads("".join(data[f"drive.verdict.{gen}.goal.{i}"] for i in range(count)))
        self.assertIsNone(rec["artifact"])
        self.assertEqual(rec["candidate"], "feat/x@abc1234")


class Review(ControlCase):
    def setUp(self):
        super().setUp()
        self.enrol("REV-SESSION", "k-rev")
        self.board.post(JOB, "Reviewer", "hello", None)   # the board exists with traffic
        report = self.work / "report.md"
        report.write_text("looks good\n", encoding="utf-8")
        self.report = report

    def test_review_posts_structured_line_and_records(self):
        with mock.patch.object(self.board, "active_agent_name", return_value="Reviewer"):
            rc, out, err = self.run_cmd("review", "--job", JOB, "--artifact", "feat/x@abc1234", "verified",
                                        "--report", str(self.report), "--key", "k-rev")
        self.assertEqual(rc, 0, err)
        self.assertRegex(self.last_line(out), r"^swarm-record: kind=review id=\d+ rev=1$")
        texts = [m.message for m in self.board.messages_after(0, JOB)]
        self.assertIn(f"REVIEW feat/x@abc1234 VERIFIED {self.report}", texts)

    def test_review_needs_enrolled_reviewer(self):
        rc, _, err = self.run_cmd("review", "--job", JOB, "--artifact", "feat/x@abc1234", "changes",
                                  "--report", str(self.report), "--key", "k-stranger")
        self.assertNotEqual(rc, 0)
        self.assertIn("enrolled agent of this job", err)

    def test_review_outcome_is_verified_or_changes_only(self):
        with self.assertRaises(SystemExit):
            cli._parser().parse_args(["review", "--job", JOB, "--artifact", "a", "maybe",
                                      "--report", str(self.report), "--key", "k-rev"])


class GoalReady(ControlCase):
    def setUp(self):
        super().setUp()
        self.req_a = self.work / "a.md"
        self.req_a.write_text("A\n", encoding="utf-8")
        self.req_b = self.work / "b.md"
        self.req_b.write_text("B\n", encoding="utf-8")

    def candidate(self, *extra):
        return self.run_cmd("goal-ready", "--job", JOB, "--candidate", "feat/x@abc1234", *extra)

    def test_goal_ready_pins_requirements_and_records(self):
        rc, out, err = self.candidate("--add-requirements", str(self.req_a))
        self.assertEqual(rc, 0, err)
        led = ledger.load(self.bkey, JOB)
        self.assertIn(os.path.realpath(self.req_a), led.pinned_requirements)
        self.assertRegex(self.last_line(out), r"^swarm-record: kind=goal_ready id=\d+ rev=1$")

    def test_goal_ready_is_a_board_message_whose_id_is_the_record(self):
        self.board.post(JOB, "Reviewer", "hello", None)   # so the GOAL-READY id is not simply 1
        rc, out, err = self.candidate("--add-requirements", str(self.req_a))
        self.assertEqual(rc, 0, err)
        req = drive_cli.requirements_digest(ledger.load(self.bkey, JOB).pinned_requirements)
        posted = [m for m in self.board.messages_after(0, JOB) if m.message.startswith("GOAL-READY ")]
        self.assertEqual([m.message for m in posted], [f"GOAL-READY feat/x@abc1234 req={req}"])
        self.assertEqual(self.last_line(out), f"swarm-record: kind=goal_ready id={posted[0].id} rev=1")

    def test_goal_ready_needs_a_requirement(self):
        rc, _, err = self.candidate()
        self.assertNotEqual(rc, 0)
        self.assertIn("at least one pinned requirement", err)

    def test_goal_ready_cannot_replace_pinned_requirements(self):
        self.assertEqual(self.candidate("--add-requirements", str(self.req_a))[0], 0)
        self.req_a.write_text("A changed\n", encoding="utf-8")
        rc, _, err = self.candidate("--add-requirements", str(self.req_a))
        self.assertNotEqual(rc, 0)
        self.assertIn("cannot be replaced", err)
        self.assertEqual(ledger.load(self.bkey, JOB).pinned_requirements[os.path.realpath(self.req_a)],
                         drive_cli.blob_id(b"A\n"))

    def test_goal_ready_adds_new_requirement_without_dropping_old(self):
        self.candidate("--add-requirements", str(self.req_a))
        self.assertEqual(self.candidate("--add-requirements", str(self.req_b))[0], 0)
        self.assertEqual(len(ledger.load(self.bkey, JOB).pinned_requirements), 2)

    def test_goal_ready_candidate_format(self):
        self.assertEqual(self.run_cmd("goal-ready", "--job", JOB, "--candidate", "not a ref",
                                      "--add-requirements", str(self.req_a))[0], 1)
        rc, out, err = self.run_cmd("goal-ready", "--job", JOB, "--candidate",
                                    "report:docs/r.md@" + "a" * 40, "--add-requirements", str(self.req_a))
        self.assertEqual(rc, 0, err)

    def test_requirement_outside_roots_refused(self):
        outside = self.home / "outside.md"
        outside.write_text("x", encoding="utf-8")
        rc, _, err = self.candidate("--add-requirements", str(outside))
        self.assertNotEqual(rc, 0)
        self.assertIn("outside the job's work roots", err)


if __name__ == "__main__":
    unittest.main()
