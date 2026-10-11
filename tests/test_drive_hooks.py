"""Drive-job hooks (spec 3.4, 3.11, 4.0, 4.5): receipts for seat requests and person-path commands,
the seat authorization the driver checks, read-only seat refusals, the target and force refusals,
the requester session's spawn refusal, and the debounced supervisor kick. Built on the hook Env."""
from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import types
from pathlib import Path
from unittest import mock

from test_hooks_cli import Env

from swarm import enrolment, hooks, receipts
from swarm.board.autoinit import store_key
from swarm.supervisor import drive_authz, ledger

ISO = "2026-10-10T00:00:00+00:00"
ABS_CWD = os.path.abspath(os.path.join(os.sep, "w"))   # absolute and normalised on every platform
LEAD_KEY = "lead-key"
SEAT_KEY = "eng-key"


class _Bound:
    """The board with running seats bound to their sessions' agent keys (what a launch claim and the
    session's enrolment do; set_seat_state cannot bind a seat without a claim, so the test binds here)."""

    def __init__(self, board, bind: dict):
        self._b, self._bind = board, bind

    def __getattr__(self, name):
        return getattr(self._b, name)

    def seats(self, job, generation=None, include_closed=True):
        return [dataclasses.replace(s, agent_key=self._bind.get(s.seat, s.agent_key))
                for s in self._b.seats(job, generation, include_closed)]

    def seat(self, job, generation, seat):
        s = self._b.seat(job, generation, seat)
        return dataclasses.replace(s, agent_key=self._bind.get(seat, s.agent_key)) if s else None


class DriveCase(Env):
    def setUp(self):
        super().setUp()
        self.key = store_key(self.cfg)
        with self.board() as b:
            b.open_job("J", None, None, None, "me", goal="G")
            self.seat_lead = b.request_seat("J", 1, "lead", role="engineering_lead", harness="claude",
                                            model="opus", workdir="/w/lead", brief_path="L.md",
                                            requested_by="person")
            self.seat_eng = b.request_seat("J", 1, "eng-b", role="engineer", harness="claude",
                                           model="haiku", workdir="/w/b", brief_path="B.md",
                                           requested_by="lead")
        self.led = ledger.create(self.key, "J", budgets={}, activated_at=ISO)
        self.led.requester_session = "req-session"
        self.led.save()
        hooks._CURRENT["host"] = None

    def seat_row(self, name: str):
        with self.board() as b:
            return b.seat("J", 1, name)

    def bind(self, agent_key: str, session: str, job: str = "J"):
        enrolment.write(self.key, job=job, agent_key=agent_key, harness="claude", session_id=session,
                        cwd=ABS_CWD)

    def run_post(self, command: str, record: str | None, *, exit_code: int = 0, sid="s-lead",
                 agent: str | None = None, bind: dict | None = None):
        stdout = (record + "\n") if record else ""
        payload = {"tool_name": "Bash", "tool_input": {"command": command},
                   "tool_response": {"exit_code": exit_code, "stdout": stdout}}
        with self.board() as b:
            return hooks.drive_post_tool(_Bound(b, bind or {}), self.cfg, payload, agent, sid)

    def receipt_for(self, kind: str, rid: str):
        return receipts.find(self.key, "J", kind, rid, rev=self.seat_lead.revision if kind == "seat_request"
                             else 1, incarnation=self.led.incarnation, generation=1, now=0.0) or \
            self._find(kind, rid)

    def _find(self, kind, rid):
        import time
        with self.board() as b:
            row = b.seat("J", 1, "eng-b") if kind == "seat_request" else None
        rev = row.revision if row else 1
        return receipts.find(self.key, "J", kind, rid, rev=rev, incarnation=self.led.incarnation,
                             generation=1, now=time.time())


class ReceiptMapping(DriveCase):
    def test_every_record_kind_maps_to_a_receipt_kind(self):
        for kind in ("done", "review", "verdict", "seat", "ask", "answer", "post", "goal_ready", "ack",
                     "withdraw", "halt", "unhalt", "deactivate", "activate"):
            with self.subTest(kind=kind):
                self.assertIn(drive_authz.receipt_kind(kind), receipts.KINDS)
        self.assertEqual(drive_authz.receipt_kind("seat"), "seat_request")
        self.assertEqual(drive_authz.receipt_kind("halt"), "person")
        self.assertIsNone(drive_authz.receipt_kind("nonsense"))

    def test_canonical_seat_args_fixed_order_and_optional_fields(self):
        row = self.seat_eng
        self.assertEqual(drive_authz.canonical_seat_args(row)[:4], ["seat", "request", "--job", "J"])
        self.assertIn("--model", drive_authz.canonical_seat_args(row))
        bare = types.SimpleNamespace(job="J", seat="x", role="engineer", harness="claude", model=None,
                                     brief_path="B", workdir="/w", title=None, task_ref=None,
                                     complexity="standard")
        self.assertNotIn("--model", drive_authz.canonical_seat_args(bare))
        self.assertNotIn("--title", drive_authz.canonical_seat_args(bare))


class SeatAuthorization(DriveCase):
    def authorize(self, row, *, argv=None, rev=None):
        argv = drive_authz.canonical_seat_args(row) if argv is None else argv
        r = receipts.Receipt(board_key=self.key, job="J", generation=1, incarnation=self.led.incarnation,
                             action_id="requester:seat", attempt=0, seat=None, kind="seat_request",
                             record_id=str(row.id), rev=row.revision if rev is None else rev,
                             content_sha=receipts.content_sha("seat_request", drive_authz.seat_decision(row)),
                             args_sha=receipts.args_sha(argv), expires_at=4_000_000_000.0)
        receipts.write(self.key, r)

    def test_receipted_current_row_is_authorized(self):
        self.authorize(self.seat_eng)
        with self.board() as b:
            self.assertTrue(drive_authz.seat_request_authorized(self.key, b, "J", 1, "eng-b", now=0.0))

    def test_row_changed_after_the_receipt_is_not_authorized(self):
        self.authorize(self.seat_eng)
        with self.board() as b:
            b.request_seat("J", 1, "eng-b", role="engineer", harness="claude", model="haiku",
                           workdir="/w/other", brief_path="B.md", requested_by="lead")
        with self.board() as b:
            self.assertFalse(drive_authz.seat_request_authorized(self.key, b, "J", 1, "eng-b", now=0.0))

    def test_receipt_whose_decision_fields_differ_is_not_authorized(self):
        """The argv hash matches the row, but the receipt's decision digest was taken of other fields."""
        other = dataclasses.replace(self.seat_eng, role="reviewer")
        r = receipts.Receipt(board_key=self.key, job="J", generation=1, incarnation=self.led.incarnation,
                             action_id="requester:seat", attempt=0, seat=None, kind="seat_request",
                             record_id=str(self.seat_eng.id), rev=self.seat_eng.revision,
                             content_sha=receipts.content_sha("seat_request", drive_authz.seat_decision(other)),
                             args_sha=receipts.args_sha(drive_authz.canonical_seat_args(self.seat_eng)),
                             expires_at=4_000_000_000.0)
        receipts.write(self.key, r)
        with self.board() as b:
            self.assertFalse(drive_authz.seat_request_authorized(self.key, b, "J", 1, "eng-b", now=0.0))

    def test_no_receipt_no_authorization(self):
        with self.board() as b:
            self.assertFalse(drive_authz.seat_request_authorized(self.key, b, "J", 1, "eng-b", now=0.0))

    def test_receipt_with_other_argv_is_not_authorized(self):
        self.authorize(self.seat_eng, argv=["seat", "request", "--job", "J", "--seat", "eng-b"])
        with self.board() as b:
            self.assertFalse(drive_authz.seat_request_authorized(self.key, b, "J", 1, "eng-b", now=0.0))


class PostToolReceipts(DriveCase):
    """The hook's receipts. The seat command parser is the CLI's (Task 6 adds `seat`); until then the
    parsed namespace is supplied here, the way the CLI will parse it."""

    def ns(self, **kw):
        base = dict(job="J", seat="eng-b", role="engineer", brief="B.md", workdir="/w/b", model="haiku")
        base.update(kw)
        return types.SimpleNamespace(**base)

    def test_seat_request_from_requester_gets_a_receipt_and_a_kick(self):
        cmd = "swarm seat request --job J --seat eng-b --role engineer --brief B.md --workdir /w/b"
        with mock.patch.object(hooks, "_parse_swarm_words", return_value=self.ns()), \
                mock.patch.object(hooks, "kick", return_value=True) as kick:
            ok = self.run_post(cmd, f"swarm-record: kind=seat id={self.seat_eng.id} rev={self.seat_eng.revision}",
                               sid="req-session")
        self.assertTrue(ok)
        kick.assert_called_once()
        with self.board() as b:
            self.assertTrue(drive_authz.seat_request_authorized(self.key, b, "J", 1, "eng-b", now=0.0))

    def intent_for(self, seat: str, action: str):
        self.led.intents.append(ledger.Intent(launch_id="L-" + seat, action_id=action, attempt=1, seat=seat,
                                              reserved_usd=0.0, reserved_minutes=0.0, locks=[], state="claiming"))
        self.led.save()

    def test_seat_request_from_lead_seat_gets_a_receipt(self):
        self.bind(LEAD_KEY, "s-lead")
        self.intent_for("lead", "1|seat|lead")
        cmd = "swarm seat request --job J --seat eng-b --role engineer --brief B.md --workdir /w/b"
        with mock.patch.object(hooks, "_parse_swarm_words", return_value=self.ns()), \
                mock.patch.object(hooks, "kick", return_value=True):
            ok = self.run_post(cmd, f"swarm-record: kind=seat id={self.seat_eng.id} rev={self.seat_eng.revision}",
                               sid="s-lead", agent=LEAD_KEY, bind={"lead": LEAD_KEY})
        self.assertTrue(ok)
        with self.board() as b:
            self.assertTrue(drive_authz.seat_request_authorized(self.key, b, "J", 1, "eng-b", now=0.0))

    def test_seat_request_from_non_lead_gets_no_receipt(self):
        self.bind(SEAT_KEY, "s-eng")
        self.intent_for("eng-b", "1|seat|eng-b")
        cmd = "swarm seat request --job J --seat eng-b --role engineer --brief B.md --workdir /w/b"
        with mock.patch.object(hooks, "_parse_swarm_words", return_value=self.ns()), \
                mock.patch.object(hooks, "kick", return_value=True):
            ok = self.run_post(cmd, f"swarm-record: kind=seat id={self.seat_eng.id} rev={self.seat_eng.revision}",
                               sid="s-eng", agent=SEAT_KEY, bind={"eng-b": SEAT_KEY})
        self.assertFalse(ok)
        with self.board() as b:
            self.assertFalse(drive_authz.seat_request_authorized(self.key, b, "J", 1, "eng-b", now=0.0))

    def test_unenrolled_session_naming_a_job_gets_no_receipt(self):
        """A session with no enrolment record can't pick a job by its own --job flag."""
        self.intent_for("lead", "1|seat|lead")
        cmd = "swarm seat request --job J --seat eng-b --role engineer --brief B.md --workdir /w/b"
        with mock.patch.object(hooks, "_parse_swarm_words", return_value=self.ns()), \
                mock.patch.object(hooks, "kick", return_value=True):
            ok = self.run_post(cmd, f"swarm-record: kind=seat id={self.seat_eng.id} rev={self.seat_eng.revision}",
                               sid="s-lead", agent=LEAD_KEY, bind={"lead": LEAD_KEY})
        self.assertFalse(ok)

    def test_row_swapped_before_the_hook_gets_no_receipt(self):
        cmd = "swarm seat request --job J --seat eng-b --role engineer --brief B.md --workdir /w/b"
        with self.board() as b:
            b.request_seat("J", 1, "eng-b", role="engineer", harness="claude", model="haiku",
                           workdir="/w/other", brief_path="B.md", requested_by="lead")
        with mock.patch.object(hooks, "_parse_swarm_words", return_value=self.ns()), \
                mock.patch.object(hooks, "kick", return_value=True):
            ok = self.run_post(cmd, f"swarm-record: kind=seat id={self.seat_eng.id} rev={self.seat_eng.revision}",
                               sid="req-session")
        self.assertFalse(ok)

    def test_argv_disagreeing_with_the_row_gets_no_receipt(self):
        """The row is the one the CLI printed (same id and revision), but the command named another
        workdir: the hook receipts only what the row says."""
        cmd = "swarm seat request --job J --seat eng-b --role engineer --brief B.md --workdir /w/x"
        with mock.patch.object(hooks, "_parse_swarm_words", return_value=self.ns(workdir="/w/x")), \
                mock.patch.object(hooks, "kick", return_value=True):
            ok = self.run_post(cmd, f"swarm-record: kind=seat id={self.seat_eng.id} rev={self.seat_eng.revision}",
                               sid="req-session")
        self.assertFalse(ok)

    def test_nonzero_exit_gets_no_receipt(self):
        cmd = "swarm seat request --job J --seat eng-b --role engineer --brief B.md --workdir /w/b"
        with mock.patch.object(hooks, "_parse_swarm_words", return_value=self.ns()):
            ok = self.run_post(cmd, f"swarm-record: kind=seat id={self.seat_eng.id} rev=1", exit_code=1,
                               sid="req-session")
        self.assertFalse(ok)

    def test_requester_person_command_is_receipted(self):
        ns = types.SimpleNamespace(job="J")
        with mock.patch.object(hooks, "_parse_swarm_words", return_value=ns), \
                mock.patch.object(hooks, "kick", return_value=True):
            ok = self.run_post("swarm unhalt --job J", "swarm-record: kind=unhalt id=9 rev=1", sid="req-session")
        self.assertTrue(ok)
        r = receipts.find(self.key, "J", "person", "9", rev=1, incarnation=self.led.incarnation,
                          generation=1, now=0.0)
        self.assertIsNotNone(r)
        self.assertTrue(receipts.matches(r, {"kind": "person", "id": "9", "rev": 1, "action": "unhalt",
                                             "target": "J"}))

    def test_foreign_deactivate_is_not_receipted(self):
        ns = types.SimpleNamespace(job="J")
        with mock.patch.object(hooks, "_parse_swarm_words", return_value=ns):
            ok = self.run_post("swarm deactivate --job J", "swarm-record: kind=deactivate id=9 rev=1",
                               sid="someone-else")
        self.assertFalse(ok)

    def test_failed_receipt_write_is_logged(self):
        ns = types.SimpleNamespace(job="J")
        with mock.patch.object(hooks, "_parse_swarm_words", return_value=ns), \
                mock.patch.object(receipts, "write", side_effect=OSError("disk gone")):
            ok = self.run_post("swarm unhalt --job J", "swarm-record: kind=unhalt id=9 rev=1", sid="req-session")
        self.assertFalse(ok)
        log = Path(os.environ["HOME"]) / ".local/share/swarm/host/receipt-failures.log"
        self.assertIn("disk gone", log.read_text())


class PreToolRefusals(DriveCase):
    def refused(self, agent_key: str, role: str, tool: str, tool_input: dict) -> bool:
        self.bind(agent_key, "s-" + agent_key)
        with self.board() as b:
            b_ = types.SimpleNamespace(seats=lambda job, gen, include_closed=True: [
                types.SimpleNamespace(role=role, agent_key=agent_key)], tool_finished=lambda k: None)
            return not hooks._gate_drive(b_, agent_key, {"tool_name": tool, "tool_input": tool_input},
                                         self.cfg)

    def test_read_only_judge_bash_write_blocked(self):
        for cmd in ("echo x > out.txt", "git commit -m x", "sed -i s/a/b/ f.py", "tee out.log"):
            with self.subTest(cmd=cmd):
                self.assertTrue(self.refused("judge-1", "judge", "Bash", {"command": cmd}))

    def test_read_only_judge_powershell_write_blocked(self):
        self.assertTrue(self.refused("judge-2", "judge", "PowerShell", {"command": "Set-Content f.txt x"}))

    def test_read_only_judge_monitor_and_mcp_write_blocked(self):
        self.assertTrue(self.refused("judge-3", "reviewer", "Monitor", {"command": "tail -f x"}))
        self.assertTrue(self.refused("judge-3", "reviewer", "mcp__jira__create_issue", {}))
        self.assertTrue(self.refused("judge-3", "reviewer", "Edit", {"file_path": "x"}))

    def test_readonly_judge_can_read_and_run_targeted_tests(self):
        self.assertFalse(self.refused("judge-4", "judge", "Bash", {"command": "git log --oneline -3"}))
        self.assertFalse(self.refused("judge-4", "judge", "Bash",
                                      {"command": "cd tests && python3 -B -m unittest test_x"}))
        self.assertFalse(self.refused("judge-4", "judge", "mcp__jira__get_issue", {}))

    def test_engineer_push_to_main_refused_and_force_refused(self):
        self.assertTrue(self.refused("eng-1", "engineer", "Bash", {"command": "git push origin HEAD:main"}))
        self.assertTrue(self.refused("eng-2", "engineer", "Bash", {"command": "git push --force origin feat/a"}))
        self.assertFalse(self.refused("eng-3", "engineer", "Bash", {"command": "git push origin feat/a"}))

    def test_sessions_of_other_jobs_are_not_refused(self):
        """An enrolled session whose job has no ledger (not a drive job) is untouched."""
        self.bind("plain-1", "s-plain", job="PLAIN")
        with self.board() as b:
            b_ = types.SimpleNamespace(seats=lambda job, gen, include_closed=True: [], tool_finished=lambda k: None)
            ok = hooks._gate_drive(b_, "plain-1", {"tool_name": "Bash",
                                                   "tool_input": {"command": "git push origin HEAD:main"}},
                                   self.cfg)
        self.assertTrue(ok)

    def test_requester_spawn_refused_in_a_drive_job(self):
        self.assertTrue(hooks._drive_requester_of(self.cfg, "req-session"))
        self.assertFalse(hooks._drive_requester_of(self.cfg, "stranger"))


class Kick(Env):
    def test_kick_debounced(self):
        calls = []
        with mock.patch("subprocess.run", side_effect=lambda *a, **k: calls.append(a) or mock.Mock(returncode=0)):
            self.assertTrue(hooks.kick(now=lambda: 100.0))
            self.assertFalse(hooks.kick(now=lambda: 110.0))
            self.assertTrue(hooks.kick(now=lambda: 121.0))
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0][:4], ["systemctl", "--user", "start", "--no-block"])

    def test_kick_never_raises(self):
        with mock.patch("subprocess.run", side_effect=OSError("no systemctl")):
            self.assertFalse(hooks.kick(now=lambda: 500.0))


class DoctorReceipts(Env):
    def test_doctor_fails_on_receipt_write_failure(self):
        from swarm import bootstrap, paths
        d = paths.host_dir()
        d.mkdir(parents=True, exist_ok=True)
        import time
        (d / "receipt-failures.log").write_text(f"{time.time():.0f} J seat_request id=7 rev=1: OSError: gone\n")
        checks = [c for c in bootstrap._receipt_failures_check() if c.name == "drive receipts"]
        self.assertEqual([c.ok for c in checks], [False])

    def test_doctor_ignores_an_old_receipt_failure(self):
        from swarm import bootstrap, paths
        d = paths.host_dir()
        d.mkdir(parents=True, exist_ok=True)
        (d / "receipt-failures.log").write_text("1000 J seat_request id=7 rev=1: OSError: old\n")
        self.assertEqual(bootstrap._receipt_failures_check(), [])
