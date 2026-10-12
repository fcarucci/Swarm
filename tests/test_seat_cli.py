"""Drive control commands (Task 6b): halt/unhalt, seat request/list/show/release/cancel, review,
goal-ready, drive repair. Handlers run through the real CLI parser against the memory board, under a temporary
HOME inside the suite sandbox. The caller is attested the way the PreToolUse hook does it
(drivegate.write_grants for the session's own shell call: the requester by the ledger's requester_session, a
seat by the context the host-private chain gives it, built here directly); the job's lead is a real `lead`
seat row (its state set on the memory store: a seat starts running only through a launch claim)."""
from __future__ import annotations

import contextlib
import io
import json
import os
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import SANDBOX, MemoryHarness, home_env, posix_only  # noqa: F401

from swarm import cli, drive_cli, drivegate, enrolment, person, review  # noqa: E402
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
        led = ledger.create(self.bkey, JOB, budgets={}, activated_at=self.board.now().isoformat())
        led.requester_session = REQ_SESSION   # what activation records (the hook's requester_context reads it)
        led.save()
        self.seat_of = {}   # session -> (job, seat) the host-private chain resolves (drivegate.seat_context)
        enrolment.write_job_owner(self.bkey, job=JOB, harness="claude", session_id=REQ_SESSION,
                                  cwd=str(self.work))
        self.roots = [str(self.work)]
        tmp = mock.patch.object(drive_cli, "TMP_ROOTS", ("/tmp-not-used-by-tests",))
        tmp.start()
        self.addCleanup(tmp.stop)
        patch = mock.patch.object(drive_cli, "_owner_roots", side_effect=lambda bk, job: list(self.roots))
        patch.start()
        self.addCleanup(patch.stop)
        self.lead_row = self.make_lead(LEAD_KEY)
        self.agent_id = None
        self.set_session(REQ_SESSION)

    def make_lead(self, key, seat="lead", state="running"):
        """A real lead seat row of generation 1 holding `key` (what a launch and the seat's enrolment make)."""
        row = self.board.request_seat(JOB, 1, seat, role="engineering_lead", harness="claude", model="opus",
                                      workdir=str(self.work), brief_path="L.md", requested_by="activation")
        store = next(r for r in self.board._s().seats if r["id"] == row.id)
        store.update(agent_key=key, state=state)
        return row

    def set_session(self, sid, env=None):
        """The session whose shell runs the next commands: its hook attests it (None: no hook, a terminal).
        `env` is what the process environment claims (default: the same session)."""
        self.session = sid
        claimed = sid if env is None else env
        p = mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": claimed} if claimed else {})
        p.start()
        self.addCleanup(p.stop)
        if not claimed:   # a terminal outside any agent host: none of the host's variables
            for var in person.HOST_ENV:
                os.environ.pop(var, None)

    SEATS = {LEAD_KEY: "lead", ENG_KEY: "eng"}

    def enrol(self, session, key, job=JOB, seat=None):
        enrolment.write(self.bkey, job=job, agent_key=key, harness="claude", session_id=session,
                        cwd=str(self.work))
        seat = seat or self.SEATS.get(key)
        if seat:
            self.seat_of[session] = (job, seat)

    def seat_ctx(self, session):
        """The drivegate.Ctx seat_context gives a seat session (marker, run file and intent, here direct)."""
        if session not in self.seat_of:
            return None
        job, seat = self.seat_of[session]
        try:
            led = ledger.load(self.bkey, job)
        except ledger.LedgerBadError:
            led = None
        intent = ledger.Intent(f"L-{seat}", f"A-{seat}", 1, seat, 0.0, 0.0, [], "claimed")
        return drivegate.Ctx(kind="seat", job=job, led=led, seat=seat if led else None,
                             intent=intent if led else None)

    def run_cmd(self, *argv):
        """Run `swarm <argv>` as the current session's shell call: its PreToolUse hook writes the grant, then
        the real CLI parser and handler run. (rc, stdout, stderr)."""
        if self.session:
            command = shlex.join(["swarm", *argv])
            payload = {"session_id": self.session, "tool_name": "Bash", "tool_input": {"command": command},
                       **({"agent_id": self.agent_id} if self.agent_id else {})}
            from swarm import hosts
            drivegate.write_grants(self.cfg, payload, hosts.get("claude"), self.seat_ctx(self.session))
        args = cli._parser().parse_args(list(argv))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(sys, "argv", ["swarm", *argv]):
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
        self.assertIn("not the requester", err)
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
        row = self.board.seat(JOB, 1, "eng")
        self.assertEqual(drivegate.record_of({"tool_response": {"stdout": out}}), ("withdraw", row.id, row.revision))

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


def goal_record(board, job, gen, candidate, req):
    """The raw stored goal record of (gen, candidate, req), bound or not (None when absent)."""
    data = board.job_data(job)
    key = review.drive_verdict_key(gen, "goal", candidate, req)
    if key not in data:
        return None
    return json.loads("".join(data[f"{key}.{i}"] for i in range(int(data[key]))))


class VerdictLine(ControlCase):
    def judge(self):
        name = self.board.allocate_name("jk", JOB)
        self.board.claim_judge("jk", JOB)
        return name

    def test_verdict_prints_record_line_with_its_message_id(self):
        judge = self.judge()
        rc, out, err = self.run_cmd("verdict", "--job", JOB, "--as", judge, "met", "all requirements hold")
        self.assertEqual(rc, 0, err)
        verdict_id = next(m.id for m in self.board.messages_after(0, JOB)
                          if m.message.startswith("VERDICT met: "))
        self.assertEqual(self.last_line(out), f"swarm-record: kind=verdict id={verdict_id} rev=1")

    def test_goal_verdict_binds_candidate_and_its_message_id(self):
        judge = self.judge()
        rc, out, err = self.run_cmd("verdict", "--job", JOB, "--as", judge, "--scope", "goal",
                                    "--candidate", "feat/x@abc1234", "--req", "d" * 64, "met", "all held")
        self.assertEqual(rc, 0, err)
        gen = ledger.load(self.bkey, JOB).generation
        posted = [m for m in self.board.messages_after(0, JOB) if m.message.startswith("VERDICT met ")]
        self.assertEqual(posted[-1].message,
                         f"VERDICT met [goal gen={gen} candidate=feat/x@abc1234 req={'d' * 64}]: all held")
        self.assertEqual(self.last_line(out), f"swarm-record: kind=verdict id={posted[-1].id} rev=1")
        rec = review.drive_verdict(self.board, JOB, gen, "goal", "feat/x@abc1234", "d" * 64)
        self.assertEqual((rec["candidate"], rec["req"], rec["message_id"]),
                         ("feat/x@abc1234", "d" * 64, posted[-1].id))
        self.assertIsNone(rec["artifact"])

    def test_the_hook_receipts_the_real_verdict_posts(self):
        """drivegate checks the VERDICT message against the submitted command (review.verdict_text both sides)."""
        judge = self.judge()
        led = ledger.load(self.bkey, JOB)
        ctx = drivegate.Ctx(kind="seat", job=JOB, led=led, seat="judge")
        for argv in (("verdict", "--job", JOB, "--as", judge, "--scope", "goal", "--candidate", "feat/x@abc1234",
                      "--req", "d" * 64, "met", "all held"),
                     ("verdict", "--job", JOB, "--as", judge, "--artifact", "feat/y@abc1234", "met", "fine")):
            rc, out, err = self.run_cmd(*argv)
            self.assertEqual(rc, 0, err)
            kind, rid, rev = drivegate.record_of({"tool_response": {"stdout": out}})
            decision = drivegate._verdict_decision(self.board, ctx, drivegate.parse_swarm(list(argv)), rid, judge)
            self.assertEqual((kind, decision["verdict"]), ("verdict", "met"))

    def test_a_verdict_from_a_non_judge_is_refused_and_posts_nothing(self):
        self.judge()
        outsider = self.board.allocate_name("outsider", JOB)
        rc, _, err = self.run_cmd("verdict", "--job", JOB, "--as", outsider, "met", "all held")
        self.assertEqual(rc, 1)
        self.assertIn("is not the judge", err)
        self.assertEqual([m for m in self.board.messages_after(0, JOB) if m.message.startswith("VERDICT ")], [])

    def test_goal_verdict_is_not_bound_to_an_artifact_named_in_its_reason(self):
        judge = self.judge()
        rc, _, err = self.run_cmd("verdict", "--job", JOB, "--as", judge, "--scope", "goal",
                                  "--candidate", "feat/x@abc1234", "--req", "d" * 64, "met",
                                  "feat/x@abc1234: all held")
        self.assertEqual(rc, 0, err)
        gen = ledger.load(self.bkey, JOB).generation
        rec = review.drive_verdict(self.board, JOB, gen, "goal", "feat/x@abc1234", "d" * 64)
        self.assertIsNone(rec["artifact"])
        self.assertEqual(rec["candidate"], "feat/x@abc1234")

    def test_P9_a_verdict_on_another_candidate_neither_replaces_nor_satisfies_it(self):
        """P9 / M5 (fixed): records are keyed by (generation, scope, candidate, req); the post names them."""
        judge = self.judge()
        self.run_cmd("verdict", "--job", JOB, "--as", judge, "--scope", "goal", "--candidate", "feat/x@abc1234",
                     "--req", "d" * 64, "not_met", "--reason", "missing B", "--next", "do B", "--open", "1")
        rc, _, err = self.run_cmd("verdict", "--job", JOB, "--as", judge, "--scope", "goal",
                                  "--candidate", "feat/evil@deadbee", "--req", "e" * 64, "met", "ok")
        self.assertEqual(rc, 0, err)
        gen = ledger.load(self.bkey, JOB).generation
        mine = review.drive_verdict(self.board, JOB, gen, "goal", "feat/x@abc1234", "d" * 64)
        self.assertEqual(mine["verdict"], "not_met")
        self.assertIsNone(review.drive_verdict(self.board, JOB, gen, "goal", "feat/x@abc1234", "e" * 64))
        last = [m.message for m in self.board.messages_after(0, JOB) if m.message.startswith("VERDICT")][-1]
        self.assertIn("candidate=feat/evil@deadbee", last)

    def test_P10_second_write_failure_leaves_no_accepted_verdict_and_says_so(self):
        """M2 / L1 (fixed): the judge changes between the two writes; the record stays unbound, no reader
        accepts it, a VERDICT-VOID is posted, and the CLI says the verdict was posted but rejected."""
        judge = self.judge()
        real = self.board.record_verdict
        calls = []

        def flaky(*a, **kw):
            calls.append(kw.get("message_id"))
            return real(*a, **kw) if len(calls) == 1 else False
        gen = ledger.load(self.bkey, JOB).generation
        with mock.patch.object(self.board, "record_verdict", side_effect=flaky):
            rc, _, err = self.run_cmd("verdict", "--job", JOB, "--as", judge, "--scope", "goal",
                                      "--candidate", "feat/x@abc1234", "--req", "d" * 64, "met", "ok")
        self.assertEqual(rc, 1)
        self.assertIn("posted but unbound", err)
        self.assertIn("rejected", err)
        self.assertNotIn("is not the judge", err)
        raw = goal_record(self.board, JOB, gen, "feat/x@abc1234", "d" * 64)
        self.assertEqual(raw["message_id"], 0)                    # stored, unbound ...
        self.assertIsNone(review.bound_verdict(raw))              # ... and no reader accepts it
        self.assertIsNone(review.drive_verdict(self.board, JOB, gen, "goal", "feat/x@abc1234", "d" * 64))
        posted = [m.message for m in self.board.messages_after(0, JOB)]
        void = [t for t in posted if t.startswith("VERDICT-VOID ")]
        self.assertEqual(len(void), 1)

    def test_an_unbound_artifact_verdict_is_not_an_artifact_verdict(self):
        """The window between the two writes: an artifact record with message_id None counts for no reader."""
        judge = self.judge()
        with mock.patch.object(self.board, "post", side_effect=RuntimeError("board gone")):
            with self.assertRaises(RuntimeError):
                from swarm import spool
                spool.deliver_verdict(self.board, JOB, judge, "met", "ok", artifact="feat/x@abc1234", generation=1)
        self.assertEqual(review.artifact_verdicts(self.board, JOB), {})

    def test_N3_an_unbound_artifact_record_does_not_count_in_a_drive_job(self):
        """R3: a record with no message id (a direct board write, a verdict while the ledger did not read) is
        no artifact verdict in a drive job, whether or not the board row claims drive."""
        judge = self.judge()
        self.board.record_verdict(JOB, judge, "met", "ok", None, "feat/x@abc1234", None)
        self.assertEqual(review.artifact_verdicts(self.board, JOB), {})                  # the ledger says drive
        self.board.set_job_data(JOB, "drive.enabled", "1")
        self.assertEqual(review.artifact_verdicts(self.board, JOB), {})                  # and so does the board
        self.assertIn("feat/x@abc1234", review.artifact_verdicts(self.board, JOB, drive=False))   # a legacy job

    def test_N3_a_job_that_is_not_driven_keeps_its_legacy_verdicts(self):
        self.board.open_job("K", "d", None, REQ_SESSION, "Francesco", goal="g")
        name = self.board.allocate_name("jk2", "K")
        self.board.claim_judge("jk2", "K")
        self.board.record_verdict("K", name, "met", "ok", None, "feat/k@abc1234", None)
        self.assertIn("feat/k@abc1234", review.artifact_verdicts(self.board, "K"))

    def test_S2_a_spooled_goal_verdict_keeps_its_generation_and_gets_its_record(self):
        from swarm import spool
        judge = self.judge()
        gen = ledger.load(self.bkey, JOB).generation
        with mock.patch.object(cli, "open_board", side_effect=OSError("down"), create=True):
            pass
        spool.spool_verdict(self.cfg, JOB, judge, "met", "ok", None, scope="goal",
                            generation=drive_cli.drive_generation(self.cfg, JOB),
                            candidate="feat/x@abc1234", req="d" * 64)
        spool.flush_spool(self.board, self.cfg)
        rec = review.drive_verdict(self.board, JOB, gen, "goal", "feat/x@abc1234", "d" * 64)
        self.assertIsNotNone(rec)
        self.assertEqual(rec["verdict"], "met")

    def test_S2_the_cli_queues_a_goal_verdict_with_its_ledger_generation(self):
        from swarm import spool
        judge = self.judge()
        args = cli._parser().parse_args(["verdict", "--job", JOB, "--as", judge, "--scope", "goal", "--candidate",
                                         "feat/x@abc1234", "--req", "d" * 64, "met", "ok"])
        with mock.patch("swarm.board.open_board", side_effect=OSError("board down")), \
                contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
            cli._run_command(self.cfg, args)
        self.assertIn("queued", out.getvalue())
        spool.flush_spool(self.board, self.cfg)
        gen = ledger.load(self.bkey, JOB).generation
        self.assertIsNotNone(review.drive_verdict(self.board, JOB, gen, "goal", "feat/x@abc1234", "d" * 64))

    def test_S2_an_unreadable_ledger_warns_that_the_verdict_cannot_conclude(self):
        hits = list(self.home.rglob(ledger._name(self.bkey, JOB)))
        hits[0].write_bytes(b"bad")
        err = io.StringIO()
        from swarm import verdict_cli
        with contextlib.redirect_stderr(err):
            self.assertIsNone(verdict_cli._drive_generation(self.cfg, JOB))
        self.assertIn("cannot conclude", err.getvalue())


class SeatAck(ControlCase):
    def setUp(self):
        super().setUp()
        self.enrol("LEAD-SESSION", LEAD_KEY)
        for i in range(20):   # board events the lead can acknowledge
            self.board.post(JOB, "Someone", f"event {i}", None)
        self.newest = self.board.last_message_id(JOB)

    def test_a_lead_successor_acks_its_own_mark(self):
        self.make_lead("k-lead2", seat="lead~1")
        self.enrol("LEAD2-SESSION", "k-lead2", seat="lead~1")
        self.set_session("LEAD2-SESSION")
        self.assertEqual(self.run_cmd("seat", "ack", "--job", JOB, "--upto", "4")[0], 0)
        self.assertEqual(ledger.load(self.bkey, JOB).watermarks, {"event:lead~1": 4})

    def test_lead_ack_moves_its_event_mark_and_prints_record(self):
        self.set_session("LEAD-SESSION")
        rc, out, err = self.run_cmd("seat", "ack", "--job", JOB, "--upto", "12")
        self.assertEqual(rc, 0, err)
        self.assertEqual(ledger.load(self.bkey, JOB).watermarks["event:lead"], 12)
        self.assertEqual(drivegate.record_of({"tool_response": {"stdout": out}}), ("ack", 12, 1))   # id = --upto

    def test_ack_never_moves_the_mark_back(self):
        self.set_session("LEAD-SESSION")
        self.assertEqual(self.run_cmd("seat", "ack", "--job", JOB, "--upto", "12")[0], 0)
        self.assertEqual(self.run_cmd("seat", "ack", "--job", JOB, "--upto", "5")[0], 0)
        self.assertEqual(ledger.load(self.bkey, JOB).watermarks["event:lead"], 12)

    def test_P5_ack_past_the_newest_event_is_refused(self):
        """H2 / S1 (fixed): the mark never goes past the board's newest message of the job."""
        self.set_session("LEAD-SESSION")
        rc, _, err = self.run_cmd("seat", "ack", "--job", JOB, "--upto", str(10 ** 18))
        self.assertEqual(rc, 1)
        self.assertIn("past the newest event", err)
        self.assertEqual(self.run_cmd("seat", "ack", "--job", JOB, "--upto", str(self.newest))[0], 0)
        self.assertNotEqual(self.run_cmd("seat", "ack", "--job", JOB, "--upto", str(self.newest + 1))[0], 0)
        self.assertEqual(ledger.load(self.bkey, JOB).watermarks["event:lead"], self.newest)

    def test_P6_a_seat_exporting_the_lead_session_id_cannot_ack(self):
        """H1 exploit 2 (fixed): the hook attests the seat's own session, whatever its environment claims."""
        self.enrol("ENG-SESSION", ENG_KEY)
        self.set_session("ENG-SESSION", env="LEAD-SESSION")
        rc, _, err = self.run_cmd("seat", "ack", "--job", JOB, "--upto", "3")
        self.assertEqual(rc, 1)
        self.assertIn("lead session", err)
        self.assertNotIn("event:lead", ledger.load(self.bkey, JOB).watermarks)

    def test_ack_from_a_person_or_another_enrolled_agent_refused(self):
        rc, _, err = self.run_cmd("seat", "ack", "--job", JOB, "--upto", "3")   # the requester session
        self.assertEqual(rc, 1)
        self.assertIn("lead session", err)
        self.enrol("ENG-SESSION", ENG_KEY)
        self.set_session("ENG-SESSION")
        rc, _, err = self.run_cmd("seat", "ack", "--job", JOB, "--upto", "3")
        self.assertEqual(rc, 1)
        self.assertIn("lead session", err)
        self.assertNotIn("event:lead", ledger.load(self.bkey, JOB).watermarks)

    def test_a_subagent_of_the_lead_cannot_ack(self):
        self.set_session("LEAD-SESSION")
        self.agent_id = "sub-of-lead"
        self.assertEqual(self.run_cmd("seat", "ack", "--job", JOB, "--upto", "3")[0], 1)

    def test_L3_a_released_lead_no_longer_counts_as_lead(self):
        store = next(r for r in self.board._s().seats if r["id"] == self.lead_row.id)
        store["state"] = "released"
        self.set_session("LEAD-SESSION")
        rc, _, err = self.run_cmd("seat", "ack", "--job", JOB, "--upto", "3")
        self.assertEqual(rc, 1)
        self.assertIn("live lead", err)

    def test_ack_needs_a_non_negative_event_id(self):
        self.set_session("LEAD-SESSION")
        rc, _, err = self.run_cmd("seat", "ack", "--job", JOB, "--upto", "-1")
        self.assertEqual(rc, 1)
        self.assertIn("--upto must be an event id", err)


class LeadAuthority(ControlCase):
    """S5: the real _lead_keys / _lead_seat_name gate (no mocks), per command, and across jobs."""
    def setUp(self):
        super().setUp()
        self.enrol("LEAD-SESSION", LEAD_KEY)
        req = self.work / "a.md"
        req.write_text("A\n", encoding="utf-8")
        self.req = req
        self.board.post(JOB, "Someone", "event", None)

    def commands(self, job=JOB):
        return {
            "seat request": ("seat", "request", "--job", job, "--seat", "eng", "--role", "engineer",
                             "--brief", "B.md", "--workdir", str(self.work)),
            "goal-ready": ("goal-ready", "--job", job, "--candidate", "feat/x@abc1234",
                           "--add-requirements", str(self.req)),
            "seat ack": ("seat", "ack", "--job", job, "--upto", "1"),
        }

    def test_the_live_lead_may_run_every_lead_command(self):
        self.set_session("LEAD-SESSION")
        for what, argv in self.commands().items():
            rc, _, err = self.run_cmd(*argv)
            self.assertEqual(rc, 0, f"{what}: {err}")
        rc, _, err = self.run_cmd("seat", "release", "--job", JOB, "--seat", "eng",
                                  "--withdraw-artifact", "feat/x", "--reason", "superseded")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.board.seat(JOB, 1, "eng").state, "released")

    def test_L3_a_released_or_failed_lead_is_refused_every_lead_command(self):
        store = next(r for r in self.board._s().seats if r["id"] == self.lead_row.id)
        for state in ("released", "failed"):
            store["state"] = state
            self.set_session("LEAD-SESSION")
            for what, argv in self.commands().items():
                rc, _, err = self.run_cmd(*argv)
                self.assertEqual(rc, 1, f"{state} {what}: {err}")
        self.assertIsNone(self.board.seat(JOB, 1, "eng"))
        self.assertEqual(ledger.load(self.bkey, JOB).pinned_requirements, {})

    def test_another_enrolled_seat_is_refused_every_lead_command(self):
        self.enrol("ENG-SESSION", ENG_KEY)
        self.set_session("ENG-SESSION")
        for what, argv in self.commands().items():
            rc, _, err = self.run_cmd(*argv)
            self.assertEqual(rc, 1, what)
        self.assertIsNone(self.board.seat(JOB, 1, "eng"))
        self.assertEqual(ledger.load(self.bkey, JOB).pinned_requirements, {})

    def test_P6_lead_session_id_exported_by_a_seat_gets_no_lead_authority(self):
        self.enrol("ENG-SESSION", ENG_KEY)
        self.set_session("ENG-SESSION", env="LEAD-SESSION")
        for what, argv in self.commands().items():
            self.assertEqual(self.run_cmd(*argv)[0], 1, what)
        self.request_seat_as_requester()
        rc, _, _ = self.run_cmd("seat", "release", "--job", JOB, "--seat", "eng",
                                "--withdraw-artifact", "g", "--reason", "r")
        self.assertEqual(rc, 1)
        self.assertNotIn("g", ledger.load(self.bkey, JOB).integrated)

    def request_seat_as_requester(self):
        sid = self.session
        self.set_session(REQ_SESSION)
        self.assertEqual(self.request_seat()[0], 0)
        self.set_session(sid, env="LEAD-SESSION")

    def test_the_lead_of_another_job_is_refused_here(self):
        self.board.open_job("K", "d", None, REQ_SESSION, "Francesco", goal="g")
        ledger.create(self.bkey, "K", budgets={}, activated_at=self.board.now().isoformat())
        enrolment.write_job_owner(self.bkey, job="K", harness="claude", session_id=REQ_SESSION, cwd=str(self.work))
        k_lead = self.board.request_seat("K", 1, "lead", role="engineering_lead", harness="claude", model="opus",
                                         workdir=str(self.work), brief_path="L.md", requested_by="activation")
        store = next(r for r in self.board._s().seats if r["id"] == k_lead.id)
        store.update(agent_key="k-lead-of-K", state="running")
        self.enrol("K-LEAD-SESSION", "k-lead-of-K", job="K", seat="lead")
        self.set_session("K-LEAD-SESSION")
        for what, argv in self.commands(JOB).items():
            self.assertEqual(self.run_cmd(*argv)[0], 1, what)
        self.assertEqual(self.run_cmd(*self.commands("K")["seat ack"])[0], 1)   # K has no events: past newest
        self.assertEqual(self.run_cmd(*self.commands("K")["seat request"])[0], 0)


class Withdraw(ControlCase):
    def setUp(self):
        super().setUp()
        self.enrol("LEAD-SESSION", LEAD_KEY)
        self.assertEqual(self.request_seat()[0], 0)
        self.set_session("LEAD-SESSION")

    def release(self, group, reason):
        return self.run_cmd("seat", "release", "--job", JOB, "--seat", "eng",
                            "--withdraw-artifact", group, "--reason", reason)

    def test_P7_an_overlong_reason_is_refused_and_nothing_is_saved(self):
        """M3 (fixed): the reason is capped; a refused release never writes the ledger."""
        rev = ledger.load(self.bkey, JOB).ledger_rev
        rc, _, err = self.release("g0", "x" * (drive_cli.WITHDRAW_REASON_MAX + 1))
        self.assertEqual(rc, 1)
        self.assertIn("--reason", err)
        self.assertEqual(ledger.load(self.bkey, JOB).ledger_rev, rev)

    def test_P7_repeating_a_release_of_a_released_seat_writes_nothing(self):
        self.assertEqual(self.release("g0", "r")[0], 0)
        rev = ledger.load(self.bkey, JOB).ledger_rev
        for i in range(5):
            rc, _, err = self.release(f"g{i + 1}", "r")
            self.assertEqual(rc, 1)
            self.assertIn("released already", err)
        led = ledger.load(self.bkey, JOB)
        self.assertEqual(led.ledger_rev, rev)
        self.assertEqual(list(led.integrated), ["g0"])

    def test_the_number_of_groups_is_capped(self):
        led = ledger.load(self.bkey, JOB)
        led.integrated = {f"g{i}": {"withdrawn": "r"} for i in range(drive_cli.INTEGRATED_MAX)}
        led.save()
        rc, _, err = self.release("one-more", "r")
        self.assertEqual(rc, 1)
        self.assertIn("groups", err)

    def test_a_ledger_over_max_bytes_is_never_written(self):
        led = ledger.load(self.bkey, JOB)
        led.integrated = {f"g{i}": {"withdrawn": "x" * 4000} for i in range(1200)}
        with self.assertRaises(ledger.LedgerTooLargeError):
            led.save()
        self.assertIsNotNone(ledger.load(self.bkey, JOB))   # the stored ledger still reads


class DriveRepair(ControlCase):
    def entry(self):
        hits = list(self.home.rglob(ledger._name(self.bkey, JOB)))
        self.assertEqual(len(hits), 1)
        return hits[0]

    def bad_entry(self, body=b"not a ledger"):
        self.entry().write_bytes(body)

    def corrupt(self, **changes):
        """Make the ledger unreadable while keeping its fields readable to a lenient parse."""
        body = json.loads(self.entry().read_text())
        body.update(changes)
        body["unknown_field"] = 1
        self.entry().write_text(json.dumps(body))

    def archives(self):
        return [p for p in self.home.rglob("*") if ".bad-" in p.name]

    def repair(self, *extra):
        return self.run_cmd("drive", "repair", "--job", JOB, *extra)

    def test_repair_moves_the_bad_ledger_aside_and_starts_a_halted_fresh_one(self):
        led = ledger.load(self.bkey, JOB)
        led.generation = led.generation_requested = 3
        led.save()
        self.corrupt()
        with self.assertRaises(ledger.LedgerBadError):
            ledger.load(self.bkey, JOB)
        rc, out, err = self.repair()
        self.assertEqual(rc, 0, err)
        self.assertRegex(self.last_line(out), r"^swarm-record: kind=halt id=\d+ rev=1$")
        fresh = ledger.load(self.bkey, JOB)
        self.assertEqual(fresh.generation, 3)
        self.assertTrue(fresh.halted["by"].startswith("person"))
        self.assertEqual(len(self.archives()), 1, "the bad entry is kept, not deleted")

    def test_P7_M4_the_generation_comes_from_the_archive_never_from_the_board(self):
        led = ledger.load(self.bkey, JOB)
        led.generation = led.generation_requested = 2
        led.save()
        self.corrupt()
        self.board.set_job_data(JOB, "drive.generation", "999")   # a database writer
        rc, _, err = self.repair()
        self.assertEqual(rc, 0, err)
        self.assertEqual(ledger.load(self.bkey, JOB).generation, 2)
        self.assertIn("the board says generation 999", err)

    def test_M4_an_unparseable_archive_needs_a_person_supplied_generation(self):
        self.board.set_job_data(JOB, "drive.generation", "3")
        self.bad_entry()
        rc, _, err = self.repair()
        self.assertEqual(rc, 1)
        self.assertIn("--generation", err)
        rc, _, err = self.repair("--generation", "3")   # the crash-window path: archived, no ledger yet
        self.assertEqual(rc, 0, err)
        self.assertEqual(ledger.load(self.bkey, JOB).generation, 3)

    def test_M4_a_person_generation_must_agree_with_the_archive(self):
        self.corrupt()
        rc, _, err = self.repair("--generation", "7")
        self.assertEqual(rc, 1)
        self.assertIn("disagrees", err)

    def test_M1_a_crash_between_the_archive_and_the_create_is_repaired_by_a_retry(self):
        led = ledger.load(self.bkey, JOB)
        led.generation = led.generation_requested = 4
        led.save()
        self.corrupt()
        real_write = ledger.privfs.write_atomic
        with mock.patch.object(ledger.privfs, "write_atomic", side_effect=OSError("crash")):
            with self.assertRaises(OSError):
                self.repair()
        self.assertIsNone(ledger.load(self.bkey, JOB))   # archived, no ledger: DRIVER-ORPHAN until repaired
        self.assertEqual(len(self.archives()), 1)
        self.assertIs(ledger.privfs.write_atomic, real_write)
        rc, out, err = self.repair()
        self.assertEqual(rc, 0, err)
        self.assertIn("finishing an interrupted repair", out)
        self.assertEqual(ledger.load(self.bkey, JOB).generation, 4)
        self.assertEqual(len(self.archives()), 1)

    def test_repair_is_idempotent_and_keeps_one_archive(self):
        self.corrupt()
        self.assertEqual(self.repair()[0], 0)
        rc, _, err = self.repair()
        self.assertEqual(rc, 1)
        self.assertIn("nothing to repair", err)
        archives = self.archives()
        self.assertEqual(len(archives), 1)
        self.assertRegex(archives[0].name, r"\.bad-\d{8}T\d{6}Z-[0-9a-f]{12}$")

    def test_repair_refuses_a_readable_ledger(self):
        rc, _, err = self.repair()
        self.assertEqual(rc, 1)
        self.assertIn("nothing to repair", err)
        self.assertIsNotNone(ledger.load(self.bkey, JOB))

    def test_repair_without_ledger_archive_or_drive_claim_refused(self):
        self.entry().unlink()
        rc, _, err = self.repair("--generation", "1")
        self.assertEqual(rc, 1)
        self.assertIn("nothing to repair", err)

    def test_repair_is_person_only(self):
        self.corrupt()
        self.enrol("ENG-SESSION", ENG_KEY)
        self.set_session("ENG-SESSION")
        rc, _, err = self.repair()
        self.assertEqual(rc, 1)
        self.assertRegex(err, "not the requester|enrolled")
        with self.assertRaises(ledger.LedgerBadError):
            ledger.load(self.bkey, JOB)

    def test_P8_S3_a_finished_job_stays_finished(self):
        self.corrupt(terminal={"bogus": True})
        rc, _, err = self.repair()
        self.assertEqual(rc, 0, err)
        fresh = ledger.load(self.bkey, JOB)
        self.assertIsNotNone(fresh.terminal)
        self.assertIsNone(fresh.halted)
        self.assertIn("stays terminal", err)
        self.assertNotIn(fresh, ledger.unfinished(self.bkey))

    def test_S3_a_valid_archived_terminal_is_kept(self):
        self.corrupt(terminal={"verdict": "met", "at": "2026-10-10T00:00:00+00:00"})
        self.assertEqual(self.repair()[0], 0)
        self.assertEqual(ledger.load(self.bkey, JOB).terminal["verdict"], "met")

    def test_S3_a_closed_board_job_with_an_unreadable_archive_stays_finished(self):
        self.assertTrue(self.board.close_job(JOB, "completed", "met: done", forced=True))
        self.assertEqual(self.board.job_status(JOB).status, "completed")
        self.bad_entry()
        rc, _, err = self.repair("--generation", "1")
        self.assertEqual(rc, 0, err)
        self.assertEqual(ledger.load(self.bkey, JOB).terminal["verdict"], "met")

    def test_P7_S4_repair_salvages_pins_and_counters_and_lists_what_was_lost(self):
        self.assertEqual(self.request_seat()[0], 0)
        led = ledger.load(self.bkey, JOB)
        led.rounds_without_progress, led.ruling_attempts = 2, 1
        led.pinned_requirements = {str(self.work / "a.md"): "a" * 40}
        led.budgets = {"max_sessions": 5.0, "max_cost_usd": 10.0}
        led.integrated = {"feat/x": {"sha": "abc"}}
        led.intents = [ledger.Intent("L1", "A1", 1, "eng", 2.5, 30.0, ["wt:x"], "claimed"),
                       ledger.Intent("L0", "A0", 1, "eng", 1.0, 30.0, [], "concluded", "ok", True, 0.75)]
        led.save()
        self.corrupt()
        rc, _, err = self.repair()
        self.assertEqual(rc, 0, err)
        fresh = ledger.load(self.bkey, JOB)
        self.assertEqual(fresh.pinned_requirements, {str(self.work / "a.md"): "a" * 40})
        self.assertEqual((fresh.rounds_without_progress, fresh.ruling_attempts), (2, 1))
        self.assertEqual((fresh.budgets, fresh.intents, fresh.integrated), ({}, [], {}))
        for text in ("WARNING", "spend: $3.25", "open launches", "L1", "budgets", "max_cost_usd",
                     "integrated groups", "feat/x", "set the budgets"):
            self.assertIn(text, err)

    def test_S4_unhalt_after_repair_wants_budgets_first(self):
        self.corrupt()
        self.assertEqual(self.repair()[0], 0)
        rc, _, err = self.run_cmd("unhalt", "--job", JOB)
        self.assertEqual(rc, 1)
        self.assertIn("set them first", err)
        led = ledger.load(self.bkey, JOB)
        led.budgets = {"max_sessions": 3.0}
        led.save()
        self.assertEqual(self.run_cmd("unhalt", "--job", JOB)[0], 0)

    def test_L2_repair_and_activation_take_the_same_lock(self):
        """An activation between the move and the create is impossible: both run under the ledger's lock."""
        self.corrupt()
        seen = []
        real = ledger._move_aside

        def move_then_activate(d, name):
            out = real(d, name)
            import threading
            t = threading.Thread(target=lambda: seen.append(
                ledger.activate(self.bkey, JOB, budgets={"max_sessions": 9.0}, activated_at="x")))
            t.start()
            t.join(0.3)
            seen.append(("alive", t.is_alive()))
            self.thread = t
            return out
        with mock.patch.object(ledger, "_move_aside", side_effect=move_then_activate):
            rc, _, err = self.repair()
        self.thread.join(10)
        self.assertEqual(rc, 0, err)
        self.assertIn(("alive", True), seen)   # the activation waited for the repair's lock
        led = ledger.load(self.bkey, JOB)
        self.assertEqual(led.generation_requested, led.generation + 1)   # it rolled the repaired ledger over
        self.assertIsNotNone(led.halted)                                  # which stays halted


class TtyReceipt(ControlCase):
    def test_codex_seat_cancel_goes_through_the_same_gate(self):
        self.assertEqual(self.request_seat()[0], 0)
        self.enrol("CODEX-SEAT", ENG_KEY)
        self.set_session(None)
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "CODEX-SEAT"}), \
                mock.patch.object(person, "_tty_confirm", return_value=True):
            rc, _, err = self.run_cmd("seat", "cancel", "--job", JOB, "--seat", "eng")
        self.assertEqual(rc, 1)
        self.assertEqual(self.board.seat(JOB, 1, "eng").state, "requested")


class PersonReceipts(ControlCase):
    """8c ruling: every successful person action writes a `person` receipt that receipts.find/matches locate,
    on the requester path and on the tty path."""
    def receipt(self, action, ref, led=None):
        from swarm import receipts
        led = led or ledger.load(self.bkey, JOB)
        rid = drive_cli.person_record_id(action, ref)
        r = receipts.find(self.bkey, JOB, "person", rid, rev=1, incarnation=led.incarnation,
                          generation=led.generation, now=0)
        self.assertIsNotNone(r, rid)
        self.assertTrue(receipts.matches(r, {"kind": "person", "id": rid, "rev": 1, "action": action,
                                             "target": JOB}))
        return r

    def as_person_at_a_terminal(self):
        self.set_session(None)
        for target, value in (("_ancestor_host", None), ("_tty_confirm", True)):
            p = mock.patch.object(person, target, return_value=value)
            p.start()
            self.addCleanup(p.stop)

    def test_requester_halt_unhalt_and_cancel_write_person_receipts(self):
        self.assertEqual(self.run_cmd("halt", "--job", JOB)[0], 0)
        rev = ledger.load(self.bkey, JOB).ledger_rev
        self.assertEqual(drive_cli.person_record_id("halt", rev), f"halt:{rev}")   # the id 8c reads
        self.assertEqual(drive_cli.person_record_id("seat cancel", 7), "seat_cancel:7")
        self.assertEqual(self.receipt("halt", rev).action_id, "requester:halt")
        self.assertEqual(self.run_cmd("unhalt", "--job", JOB)[0], 0)
        self.receipt("unhalt", ledger.load(self.bkey, JOB).ledger_rev)
        self.assertEqual(self.request_seat()[0], 0)
        rc, out, err = self.run_cmd("seat", "cancel", "--job", JOB, "--seat", "eng")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.receipt("seat cancel", self.board.seat(JOB, 1, "eng").id).action_id,
                         "requester:seat_cancel")

    def test_tty_halt_and_repair_write_person_receipts(self):
        self.as_person_at_a_terminal()
        rc, _, err = self.run_cmd("halt", "--job", JOB)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.receipt("halt", ledger.load(self.bkey, JOB).ledger_rev).action_id, "tty:halt")
        body = json.loads(next(self.home.rglob(ledger._name(self.bkey, JOB))).read_text())
        body["x"] = 1
        next(self.home.rglob(ledger._name(self.bkey, JOB))).write_text(json.dumps(body))
        rc, _, err = self.run_cmd("drive", "repair", "--job", JOB)
        self.assertEqual(rc, 0, err)
        fresh = ledger.load(self.bkey, JOB)
        self.assertEqual(self.receipt("drive repair", fresh.ledger_rev, fresh).action_id, "tty:drive_repair")

    def test_a_person_deactivate_writes_a_receipt_the_driver_reads_as_a_cancel(self):
        self.as_person_at_a_terminal()
        args = cli._parser().parse_args(["deactivate", "--job", JOB, "--status", "cancelled"])
        with mock.patch("swarm.board.open_board", return_value=self.board), \
                mock.patch.object(self.board, "close", lambda: None), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            cli._parser()   # registration wraps COMMANDS["deactivate"] (drive_cli.register)
            self.assertEqual(cli.COMMANDS["deactivate"](self.cfg, args), 0)
        led = ledger.load(self.bkey, JOB)
        self.assertEqual(self.receipt("deactivate", led.generation).action_id, "tty:deactivate")

    def test_an_agent_deactivate_writes_no_person_receipt(self):
        from swarm import receipts
        self.enrol("ENG-SESSION", ENG_KEY)
        self.set_session("ENG-SESSION")
        args = cli._parser().parse_args(["deactivate", "--job", JOB, "--status", "cancelled"])
        with mock.patch("swarm.board.open_board", return_value=self.board), \
                mock.patch.object(self.board, "close", lambda: None), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            cli._parser()
            cli.COMMANDS["deactivate"](self.cfg, args)
        led = ledger.load(self.bkey, JOB)
        self.assertIsNone(receipts.find(self.bkey, JOB, "person", drive_cli.person_record_id("deactivate", 1),
                                        rev=1, incarnation=led.incarnation, generation=led.generation, now=0))


class Probes(ControlCase):
    """The Task 6b security probes P1 and P6 against the real CLI (P2-P5, P7-P10 live above and in test_person)."""
    def test_P1_enrolled_seat_exporting_the_requester_session_id_is_refused(self):
        self.enrol("ENG-SESSION", ENG_KEY)
        self.set_session("ENG-SESSION")
        self.assertNotEqual(self.run_cmd("halt", "--job", JOB)[0], 0)
        owner_sid = enrolment.find_job_owner(self.bkey, JOB).session_id
        self.set_session("ENG-SESSION", env=owner_sid)
        for argv in (("halt", "--job", JOB), ("unhalt", "--job", JOB), ("drive", "repair", "--job", JOB)):
            rc, _, err = self.run_cmd(*argv)
            self.assertEqual(rc, 1, argv)
        self.assertIsNone(ledger.load(self.bkey, JOB).halted)


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
